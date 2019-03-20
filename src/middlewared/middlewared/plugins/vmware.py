from collections import OrderedDict

import errno
import socket
import ssl

from middlewared.schema import Dict, Int, Str, accepts
from middlewared.service import CallError, CRUDService, private

from pyVim import connect
from pyVmomi import vim, vmodl


class VMWareService(CRUDService):

    class Config:
        datastore = 'storage.vmwareplugin'
        datastore_extend = 'vmware.item_extend'

    @private
    async def item_extend(self, item):
        try:
            item['password'] = await self.middleware.call('notifier.pwenc_decrypt', item['password'])
        except:
            self.logger.warn('Failed to decrypt password', exc_info=True)
        return item

    @accepts(Dict(
        'vmware-creds',
        Str('hostname'),
        Str('username'),
        Str('password'),
    ))
    def get_datastores(self, data):
        """
        Get datastores from VMWare.
        """
        try:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
            ssl_context.verify_mode = ssl.CERT_NONE
            server_instance = connect.SmartConnect(
                host=data['hostname'],
                user=data['username'],
                pwd=data['password'],
                sslContext=ssl_context,
            )
        except (vim.fault.InvalidLogin, vim.fault.NoPermission, vim.fault.RestrictedVersion) as e:
            raise CallError(e.msg, errno.EPERM)
        except vmodl.RuntimeFault as e:
            raise CallError(e.msg)
        except (socket.gaierror, socket.error, OSError) as e:
            raise CallError(str(e), e.errno)

        content = server_instance.RetrieveContent()
        objview = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )

        esxi_hosts = objview.view
        objview.Destroy()

        datastores = {}
        for esxi_host in esxi_hosts:
            storage_system = esxi_host.configManager.storageSystem
            datastores_host = {}

            if storage_system.fileSystemVolumeInfo is None:
                continue

            for host_mount_info in storage_system.fileSystemVolumeInfo.mountInfo:
                if host_mount_info.volume.type == 'VMFS':
                    datastores_host[host_mount_info.volume.name] = {
                        'type': host_mount_info.volume.type,
                        'uuid': host_mount_info.volume.uuid,
                        'capacity': host_mount_info.volume.capacity,
                        'vmfs_version': host_mount_info.volume.version,
                        'local': host_mount_info.volume.local,
                        'ssd': host_mount_info.volume.ssd
                    }
                elif host_mount_info.volume.type == 'NFS':
                    datastores_host[host_mount_info.volume.name] = {
                        'type': host_mount_info.volume.type,
                        'capacity': host_mount_info.volume.capacity,
                        'remote_host': host_mount_info.volume.remoteHost,
                        'remote_path': host_mount_info.volume.remotePath,
                        'remote_hostnames': host_mount_info.volume.remoteHostNames,
                        'username': host_mount_info.volume.userName,
                    }
                elif host_mount_info.volume.type == 'OTHER':
                    # Ignore OTHER type, it does not seem to be meaningful
                    pass
                else:
                    self.logger.debug(f'Unknown volume type "{host_mount_info.volume.type}": {host_mount_info.volume}')
                    continue
            datastores[esxi_host.name] = datastores_host

        connect.Disconnect(server_instance)

        # Datastore names are unique among different esxi host under a single vcenter server. As we only require
        # datastore names and additional information is not needed for datastore choices, we refine the datastores
        # dict and send back only the datastore names

        return OrderedDict({
            name: name for name in sorted(
                name for host in datastores.values() for name in host
            )
        })

    @accepts(Int('pk'))
    async def get_virtual_machines(self, pk):

        item = await self.query([('id', '=', pk)], {'get': True})

        ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        ssl_context.verify_mode = ssl.CERT_NONE
        server_instance = connect.SmartConnect(
            host=item['hostname'],
            user=item['username'],
            pwd=item['password'],
            sslContext=ssl_context,
        )

        content = server_instance.RetrieveContent()
        objview = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        vm_view = objview.view
        objview.Destroy()

        vms = {}
        for vm in vm_view:
            data = {
                'uuid': vm.config.uuid,
                'name': vm.name,
                'power_state': vm.summary.runtime.powerState,
            }
            vms[vm.config.uuid] = data
        return vms
