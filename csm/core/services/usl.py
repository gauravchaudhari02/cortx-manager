#!/usr/bin/env python3

"""
 ****************************************************************************
 Filename:          usl.py
 Description:       Services for USL calls

 Creation Date:     10/21/2019
 Author:            Alexander Voronov

 Do NOT modify or remove this copyright and confidentiality notice!
 Copyright (c) 2001 - $Date: 2015/01/14 $ Seagate Technology, LLC.
 The code contained herein is CONFIDENTIAL to Seagate Technology, LLC.
 Portions are also trade secret. Any use, duplication, derivation, distribution
 or disclosure of this code, for any reason, not expressly authorized is
 prohibited. All other rights are expressly reserved by Seagate Technology, LLC.
 ****************************************************************************
"""

from aiohttp import ClientSession
from random import SystemRandom
from typing import Any, Dict, List
from uuid import UUID, uuid5, NAMESPACE_URL
import asyncio
import time
import salt.config
import salt.minion
import toml

from csm.common.errors import CsmError, CsmInternalError, CsmNotFoundError
from csm.common.log import Log
from csm.common.services import ApplicationService
from csm.core.blogic import const
from csm.core.blogic.models.s3 import S3ConnectionConfig
from csm.core.blogic.models.usl import Device, Volume, MountResponse

DEFAULT_EOS_DEVICE_NAME = 'cloudstore'
DEFAULT_EOS_DEVICE_VENDOR = 'Seagate'

class UslService(ApplicationService):
    """
    Implements USL service operations.
    """
    # FIXME improve token management
    _token: str
    _fake_system_serial_number: str
    _s3cli: Any
    _device: Device
    _volumes: Dict[str, Dict[str, Any]]

    def __init__(self, s3_plugin) -> None:
        """
        Constructor.
        """
        self._token = ''
        self._fake_system_serial_number = 'EES%012d' % time.time()
        self._s3cli = self._create_s3cli(s3_plugin)
        self._device = Device(DEFAULT_EOS_DEVICE_NAME, '0000', self._fake_system_serial_number,
            'internal', self._get_device_uuid(), DEFAULT_EOS_DEVICE_VENDOR)
        self._volumes = {}
        self._buckets = {}

    def _create_s3cli(self, s3_plugin):
        """Hard coded workaround to pass S3 server details to USL. Only for CES!"""

        toml_conf = toml.load(const.USL_S3_CONF)
        s3_conf = S3ConnectionConfig()
        s3_conf.host = toml_conf['server']['host']
        s3_conf.port = toml_conf['server']['port']
        return s3_plugin.get_s3_client(toml_conf['credentials']['access_key_id'],
            toml_conf['credentials']['secret_key'], s3_conf)

    def _get_device_uuid(self) -> UUID:
        """Obtains the EOS device UUID from config."""

        opts = salt.config.minion_config('/etc/salt/minion')
        grains = salt.loader.grains(opts)
        return UUID(grains['cluster_id'])

    def _get_volume_uuid(self, bucket_name: str) -> UUID:
        """Generates the EOS volume (bucket) UUID from EOS device UUID and bucket name."""

        return uuid5(self._device.uuid, bucket_name)

    async def _update_volumes_cache(self):
        """
        Updates the internal buckets cache.

        Obtains the fresh buckets list from S3 server and updates cache with it.
        Keeps cache the same if the server is not available.
        """

        try:
            buckets = await self._s3cli.get_all_buckets()

            fresh_buckets =  [b.name for b in buckets]
            cached_buckets = [v['bucketName'] for _,v in self._volumes.items()]

            # Remove staled volumes
            self._volumes = {k:v for k,v in self._volumes.items()
                             if v['bucketName'] in fresh_buckets}
            # Add new volumes
            for b in fresh_buckets:
                if not b in cached_buckets:
                    volume_uuid = self._get_volume_uuid(b)
                    self._volumes[volume_uuid] = {'bucketName' : b,
                                                  'volume' : Volume(self._device.uuid, 's3', 0, 0,
                                                                    volume_uuid)}
        except Exception as e:
            raise CsmInternalError(desc=f'Unable to update buckets cache: {str(e)}')


    async def get_device_list(self) -> List[Dict[str, str]]:
        """
        Provides a list with all available devices.

        :return: A list with dictionaries, each containing information about a specific device.
        """
        return [vars(self._device)]

    async def get_device_volumes_list(self, device_id: UUID) -> List[Dict[str, Any]]:
        """
        Provides a list of all volumes associated to a specific device.

        :param device_id: Device UUID
        :return: A list with dictionaries, each containing information about a specific volume.
        """
        if device_id != self._device.uuid:
            raise CsmNotFoundError(desc=f'Device with ID {device_id} is not found')
        await self._update_volumes_cache()
        return [vars(v['volume']) for uuid,v in self._volumes.items()]

    async def post_device_volume_mount(self, device_id: UUID, volume_id: UUID) -> Dict[str, str]:
        """
        Attaches a volume associated to a specific device to a mount point.

        :param device_id: Device UUID
        :param volume_id: Volume UUID
        :return: A dictionary containing the mount handle and the mount path.
        """
        if device_id != self._device.uuid:
            raise CsmNotFoundError(desc=f'Device with ID {device_id} is not found')

        if not volume_id in self._volumes:
            await self._update_volumes_cache()
            if not volume_id in self._volumes:
                raise CsmNotFoundError(desc=f'Volume {volume_id} is not found')
        return vars(MountResponse('handle', '/mnt', self._volumes[volume_id]['bucketName']))

    # TODO replace stub
    async def post_device_volume_unmount(self, device_id: UUID, volume_id: UUID) -> str:
        """
        Detaches a volume associated to a specific device from its current mount point.

        The current implementation reflects the API specification but does nothing.

        :param device_id: Device UUID
        :param volume_id: Volume UUID
        :return: The volume's mount handle
        """
        return 'handle'

    async def register_device(self, url: str, pin: str) -> None:
        """
        Executes device registration sequence. Communicates with the UDS server in order to start
        registration and verify its status.

        :param url: Registration URL as provided by the UDX portal
        :param pin: Registration PIN as provided by the UDX portal
        """
        # TODO use a single client session object; manage life cycle correctly
        async with ClientSession() as session:
            endpoint_url = const.UDS_SERVER_URL + '/uds/v1/registration/RegisterDevice'
            params = {'url': url, 'regPin': pin, 'regToken': self._token}
            async with session.put(endpoint_url, params=params) as response:
                Log.info(f'Start device registration at {const.UDS_SERVER_URL}')
                if response.status != 201:
                    desc = 'Could not start device registration (status code {response.status})'
                    Log.error(desc)
                    raise CsmError(desc=desc)
                Log.info('Device registration is in process---waiting for confirmation')
            # Confirm registration with a exponential backoff strategy, 10 attempts
            for n in range(10):
                wait_seconds = 0b1 << n
                Log.info(f'Attempt #{n + 1} to confirm device registration, wait {wait_seconds}s')
                await asyncio.sleep(wait_seconds)
                async with session.get(endpoint_url) as response:
                    if response.status == 201:
                        continue
                    elif response.status == 200:
                        Log.info('Device was successfully registered.')
                        break
                    desc = f'Device registration failed (status code {response.status})'
                    Log.error(desc)
                    raise CsmError(desc=desc)
            else:
                desc = 'Could not confirm device registration status'
                Log.error(desc)
                raise CsmError(desc=desc)

    # TODO replace stub
    async def get_registration_token(self) -> Dict[str, str]:
        """
        Generates a random registration token.

        :return: A 12-digit token.
        """
        self._token = ''.join(SystemRandom().sample('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ', 12))
        return {'registrationToken': self._token}

    # TODO replace stub
    async def get_system(self) -> Dict[str, str]:
        """
        Provides information about the system.

        :return: A dictionary containing system information.
        """
        return {
            'model': 'EES',
            'type': 'ees',
            'serialNumber': self._fake_system_serial_number,
            'friendlyName': 'EESFakeSystem',
            'firmwareVersion': '0.00',
        }

    # TODO replace stub
    async def get_network_interfaces(self) -> List[Dict[str, Any]]:
        """
        Provides a list of all network interfaces in a system.

        :return: A list containing dictionaries, each containing information about a specific
            network interface.
        """
        return [
            {
                'name': 'tbd',
                'type': 'tbd',
                'macAddress': 'AA:BB:CC:DD:EE:FF',
                'isActive': True,
                'isLoopback': False,
                'ipv4': '127.0.0.1',
                'netmask': '255.0.0.0',
                'broadcast': '127.255.255.255',
                'gateway': '127.255.255.254',
                'ipv6': '::1',
                'link': 'tbd',
                'duplex': 'tbd',
                'speed': 0,
            }
        ]
