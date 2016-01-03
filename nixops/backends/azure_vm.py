# -*- coding: utf-8 -*-
# This file is named azure_vm.py instead of azure.py to avoid a namespace clash with azure library

import os
import sys
import socket
import struct
import azure
import re
import base64
import random
import threading

from azure.storage.blob import BlobService
from azure.servicemanagement import *
from azure.servicemanagement._serialization import _XmlSerializer

import nixops
from nixops import known_hosts
from nixops.util import wait_for_tcp_port, ping_tcp_port
from nixops.util import attr_property, create_key_pair, generate_random_string, check_wait
from nixops.nix_expr import Call, RawValue

from nixops.backends import MachineDefinition, MachineState
from nixops.azure_common import ResourceDefinition, ResourceState

from xml.etree import ElementTree

from azure.mgmt.network import PublicIpAddress, NetworkInterface, NetworkInterfaceIpConfiguration, IpAllocationMethod, ResourceId
from azure.mgmt.compute import *

def device_name_to_lun(device):
    match = re.match(r'/dev/disk/by-lun/(\d+)$', device)
    return  None if match is None or int(match.group(1))>31 else int(match.group(1) )

def lun_to_device_name(lun):
    return ('/dev/disk/by-lun/' + str(lun))

def defn_find_root_disk(block_device_mapping):
   return next((d_id for d_id, d in block_device_mapping.iteritems()
                  if d['device'] == '/dev/sda'), None)

# when we look for the root disk in the deployed state,
# we must check that the disk is actually attached,
# because an unattached old root disk may still be around
def find_root_disk(block_device_mapping):
   return next((d_id for d_id, d in block_device_mapping.iteritems()
                  if d['device'] == '/dev/sda' and not d.get('needs_attach', False)), None)

def parse_blob_url(blob):
    match = re.match(r'https?://([^\./]+)\.[^/]+/([^/]+)/(.+)$', blob)
    return None if match is None else {
        "storage": match.group(1),
        "container": match.group(2),
        "name": match.group(3)
    }


class AzureDefinition(MachineDefinition, ResourceDefinition):
    """
    Definition of an Azure machine.
    """
    @classmethod
    def get_type(cls):
        return "azure"

    def __init__(self, xml):
        MachineDefinition.__init__(self, xml)

        x = xml.find("attrs/attr[@name='azure']/attrs")
        assert x is not None

        self.copy_option(x, 'machineName', str)

        self.copy_option(x, 'subscriptionId', str)
        self.authority_url = self.copy_option(x, 'authority', str, empty = True, optional = True)
        self.copy_option(x, 'user', str, empty = True, optional = True)
        self.copy_option(x, 'password', str, empty = True, optional = True)

        self.copy_option(x, 'size', str, empty = False)
        self.copy_option(x, 'location', str, empty = False)
        self.copy_option(x, 'storage', 'resource')
        self.copy_option(x, 'virtualNetwork', 'resource')
        self.copy_option(x, 'resourceGroup', 'resource')

        self.copy_option(x, 'rootDiskImageUrl', str, empty = False)
        self.copy_option(x, 'baseEphemeralDiskUrl', str, optional = True)

        self.obtain_ip = self.get_option_value(x, 'obtainIP', bool)
        self.copy_option(x, 'availabilitySet', str)

        def opt_disk_name(dname):
            return ("{0}-{1}".format(self.machine_name, dname) if dname is not None else None)

        def parse_block_device(xml):
            disk_name = self.get_option_value(xml, 'name', str)

            media_link = self.get_option_value(xml, 'mediaLink', str, optional = True)
            if not media_link and self.base_ephemeral_disk_url:
                media_link = "{0}{1}-{2}.vhd".format(self.base_ephemeral_disk_url,
                                                self.machine_name, disk_name)
            if not media_link:
                raise Exception("{0}: ephemeral disk {1} must specify mediaLink"
                                .format(self.machine_name, disk_name))
            blob = parse_blob_url(media_link)
            if not blob:
                raise Exception("{0}: malformed BLOB URL {1}"
                                .format(self.machine_name, media_link))
            if media_link[:5] == 'http:':
                raise Exception("{0}: please use https in BLOB URL {1}"
                                .format(self.machine_name, media_link))
            if self.storage != blob['storage']:
                raise Exception("{0}: expected storage to be {1} in BLOB URL {2}"
                                .format(self.machine_name, self.storage, media_link))
            return {
                'name': disk_name,
                'device': xml.get("name"),
                'media_link': media_link,
                'size': self.get_option_value(xml, 'size', int, optional = True),
                'is_ephemeral': self.get_option_value(xml, 'isEphemeral', bool),
                'host_caching': self.get_option_value(xml, 'hostCaching', str),
                'encrypt': self.get_option_value(xml, 'encrypt', bool),
                'passphrase': self.get_option_value(xml, 'passphrase', str)
            }

        devices = [ parse_block_device(_d)
                    for _d in x.findall("attr[@name='blockDeviceMapping']/attrs/attr") ]
        self.block_device_mapping = { _d['media_link']: _d for _d in devices }

        media_links = [ _d['media_link'] for _d in devices ]
        if len(media_links) != len(set(media_links)):
            raise Exception("{0} has duplicate disk BLOB URLs".format(self.machine_name))

        for d_id, disk in self.block_device_mapping.iteritems():
            if disk['device'] != "/dev/sda" and device_name_to_lun(disk['device']) is None:
                raise Exception("{0}: blockDeviceMapping only supports /dev/sda and "
                                "/dev/disk/by-lun/X block devices, where X is in 0..31 range"
                                .format(self.machine_name))
        if defn_find_root_disk(self.block_device_mapping) is None:
            raise Exception("{0} needs a root disk".format(self.machine_name))

    def show_type(self):
        return "{0} [{1}; {2}]".format(self.get_type(), self.location or "???", self.size or "???")


class AzureState(MachineState, ResourceState):
    """
    State of an Azure machine.
    """
    @classmethod
    def get_type(cls):
        return "azure"

    machine_name = attr_property("azure.name", None)
    public_ipv4 = attr_property("publicIpv4", None)

    size = attr_property("azure.size", None)
    location = attr_property("azure.location", None)

    public_client_key = attr_property("azure.publicClientKey", None)
    private_client_key = attr_property("azure.privateClientKey", None)

    public_host_key = attr_property("azure.publicHostKey", None)
    private_host_key = attr_property("azure.privateHostKey", None)

    storage = attr_property("azure.storage", None)
    virtual_network = attr_property("azure.virtualNetwork", None)
    resource_group = attr_property("azure.resourceGroup", None)

    obtain_ip = attr_property("azure.obtainIP", None, bool)
    availability_set = attr_property("azure.availabilitySet", None)

    block_device_mapping = attr_property("azure.blockDeviceMapping", {}, 'json')
    generated_encryption_keys = attr_property("azure.generatedEncryptionKeys", {}, 'json')

    backups = attr_property("azure.backups", {}, 'json')

    public_ip = attr_property("azure.publicIP", None)
    network_interface = attr_property("azure.networkInterface", None)

    def __init__(self, depl, name, id):
        MachineState.__init__(self, depl, name, id)
        ResourceState.__init__(self, depl, name, id)
        self._sms = None
        self._bs = None

    @property
    def resource_id(self):
        return self.machine_name

    def show_type(self):
        s = super(AzureState, self).show_type()
        return "{0} [{1}; {2}]".format(s, self.location or "???", self.size or "???")

    credentials_prefix = "deployment.azure"

    @property
    def full_name(self):
        return "Azure machine '{0}'".format(self.machine_name)

    def bs(self):
        if not self._bs:
            storage_resource = next((r for r in self.depl.resources.values()
                                       if getattr(r, 'storage_name', None) == self.storage), None)
            self._bs = BlobService(self.storage, storage_resource.access_key)
        return self._bs

    # delete_vhd = None: ask the user
    def _delete_volume(self, media_link, disk_name = None, delete_vhd = None):
        if media_link is None:
            self.warn("attempted to delete a disk without a BLOB URL; this is a bug")
            return
        try:
            if delete_vhd or (delete_vhd is None and
                              self.depl.logger.confirm("are you sure you want to destroy "
                                                       "the contents(BLOB) of Azure disk {0}({1})?"
                                                       .format(disk_name, media_link)) ):
                self.log("destroying Azure disk BLOB {0}...".format(media_link))
                blob = parse_blob_url(media_link)
                if blob is None:
                    raise Exception("failed to parse BLOB URL {0}".format(media_link))
                if blob["storage"] != self.storage:
                    raise Exception("storage {0} provided in the deployment specification "
                                    "doesn't match the storage of BLOB {1}"
                                    .format(self.storage, media_link))
                self.bs().delete_blob(blob["container"], blob["name"])
            else:
                self.log("keeping the Azure disk BLOB {0}...".format(media_link))

        except azure.common.AzureMissingResourceHttpError:
            self.warn("seems to have been destroyed already")

    def blob_exists(self, media_link):
        try:
            blob = parse_blob_url(media_link)
            if blob["storage"] != self.storage:
                raise Exception("storage {0} provided in the deployment specification "
                                "doesn't match the storage of BLOB {1}"
                                .format(self.storage, media_link))
            if blob is None:
                raise Exception("failed to parse BLOB URL {0}".format(media_link))
            self.bs().get_blob_properties(blob["container"], blob["name"])
            return True
        except azure.common.AzureMissingResourceHttpError:
            return False

    def _delete_encryption_key(self, disk_id):
        if self.generated_encryption_keys.get(disk_id, None) == None:
            return
        if self.depl.logger.confirm("Azure disk {0} has an automatically generated encryption key; "
                                    "if the key is deleted, the data will be lost even if you have "
                                    "a copy of the disk contents; "
                                    "are you sure you want to delete the encryption key?"
                                   .format(disk_id) ):
            self.update_generated_encryption_keys(disk_id, None)


    def _node_deleted(self):
        self.vm_id = None
        self.state = self.STOPPED
        for d_id, disk in self.block_device_mapping.iteritems():
            disk['needs_attach'] = True
            self.update_block_device_mapping(d_id, disk)
        ssh_host_port = self.get_ssh_host_port()
        if ssh_host_port and self.public_host_key:
            known_hosts.remove(ssh_host_port, self.public_host_key)
        self.public_ipv4 = None


    defn_properties = [ 'size', 'obtain_ip', 'availability_set' ]

    def is_deployed(self):
        return (self.vm_id or self.block_device_mapping or self.public_ip or self.network_interface)

    def get_resource(self):
        try:
            vm = self.cmc().virtual_machines.get(self.resource_group, self.resource_id).virtual_machine
            # workaround: if set to [], azure throws an error if we reuse the VM object in update requests
            if vm.extensions == []:
                vm.extensions = None
            return vm
        except azure.common.AzureMissingResourceHttpError:
            return None

    # retrieve the VM resource and complain to the user if it doesn't exist
    def get_vm_assert_exists(self):
        vm = self.get_settled_resource()
        if not vm:
            raise Exception("{0} has been deleted behind our back; "
                            "please run 'deploy --check' to fix this"
                            .format(self.full_name))
        return vm

    def destroy_resource(self):
        self.cmc().virtual_machines.delete(self.resource_group, self.resource_id)

    def is_settled(self, resource):
        return True

    def fetch_public_ip(self):
        return self.nrpc().public_ip_addresses.get(
                   self.resource_group, self.public_ip).public_ip_address.ip_address

    def update_block_device_mapping(self, k, v):
        x = self.block_device_mapping
        if v == None:
            x.pop(k, None)
        else:
            x[k] = v
        self.block_device_mapping = x

    def update_generated_encryption_keys(self, k, v):
        x = self.generated_encryption_keys
        if v == None:
            x.pop(k, None)
        else:
            x[k] = v
        self.generated_encryption_keys = x


    def create(self, defn, check, allow_reboot, allow_recreate):
        self.no_change(self.machine_name != defn.machine_name, "instance name")
        self.no_property_change(defn, 'resource_group')
        self.no_property_change(defn, 'virtual_network')
        self.no_property_change(defn, 'storage')
        self.no_property_change(defn, 'location')

        self.set_common_state(defn)
        self.copy_mgmt_credentials(defn)
        self.machine_name = defn.machine_name
        self.storage = defn.storage
        self.resource_group = defn.resource_group
        self.virtual_network = defn.virtual_network
        self.location = defn.location

        if not self.public_client_key:
            (private, public) = create_key_pair()
            self.public_client_key = public
            self.private_client_key = private

        if not self.public_host_key:
            host_key_type = "ed25519" if self.state_version != "14.12" and nixops.util.parse_nixos_version(defn.config["nixosRelease"]) >= ["15", "09"] else "ecdsa"
            (private, public) = create_key_pair(type=host_key_type)
            self.public_host_key = public
            self.private_host_key = private

        if check:
            vm = self.get_settled_resource()
            if vm:
                if self.vm_id:
                    if vm.provisioning_state == ProvisioningStateTypes.failed:
                        self.warn("vm resource exists, but is in a failed state")
                    self.handle_changed_property('size', vm.hardware_profile.virtual_machine_size)
                    self.handle_changed_property('public_ipv4', self.fetch_public_ip())
                    self.update_ssh_known_hosts()

                    # check the root disk
                    os_disk_res_name = "OS disk of {0}".format(self.full_name)
                    _root_disk_id = find_root_disk(self.block_device_mapping)
                    assert _root_disk_id is not None
                    root_disk = self.block_device_mapping[_root_disk_id]
                    self.warn_if_changed(root_disk["host_caching"], vm.storage_profile.os_disk.caching, "host_caching",
                                         resource_name = os_disk_res_name, can_fix = False)
                    self.warn_if_changed(root_disk["name"], vm.storage_profile.os_disk.name, "name",
                                         resource_name = os_disk_res_name, can_fix = False)
                    self.warn_if_changed(root_disk["media_link"], vm.storage_profile.os_disk.virtual_hard_disk.uri, "media_link",
                                         resource_name = os_disk_res_name, can_fix = False)
                    self.update_block_device_mapping(_root_disk_id, root_disk)

                    # check data disks
                    for d_id, disk in self.block_device_mapping.iteritems():
                        disk_lun = device_name_to_lun(disk['device'])
                        if disk_lun is None: continue
                        vm_disk = next((_vm_disk for _vm_disk in vm.storage_profile.data_disks
                                                 if _vm_disk.virtual_hard_disk.uri == disk['media_link']), None)
                        if vm_disk is not None:
                            disk_res_name = "data disk {0}({1})".format(disk['name'], d_id)
                            disk["host_caching"] = self.warn_if_changed(disk["host_caching"], vm_disk.caching,
                                                                        "host_caching", resource_name = disk_res_name)
                            disk["size"] = self.warn_if_changed(disk["size"], vm_disk.disk_size_gb,
                                                                "size", resource_name = disk_res_name)
                            self.warn_if_changed(disk["name"], vm_disk.name,
                                                 "name", resource_name = disk_res_name, can_fix = False)
                            if disk.get("needs_attach", False):
                                self.warn("disk {0}({1}) was not supposed to be attached".format(disk['name'], d_id))
                                disk["needs_attach"] = False

                            if vm_disk.lun != disk_lun:
                                self.warn("disk {0}({1}) is attached to this instance at a "
                                          "wrong LUN {2} instead of {3}"
                                          .format(disk['name'], disk['media_link'], vm_disk.lun, disk_lun))
                                self.log("detaching disk {0}({1})...".format(disk['name'], disk['media_link']))
                                vm.storage_profile.data_disks.remove(vm_disk)
                                self.cmc().virtual_machines.create_or_update(self.resource_group, vm)
                                disk["needs_attach"] = True
                        else:
                            if not disk.get('needs_attach', False):
                                self.warn("disk {0}({1}) has been unexpectedly detached".format(disk['name'], d_id))
                                disk["needs_attach"] = True
                            if not self.blob_exists(disk['media_link']):
                                self.warn("disk BLOB {0}({1}) has been unexpectedly deleted".format(disk['name'], d_id))
                                disk = None

                        self.update_block_device_mapping(d_id, disk)

                    # detach "unexpected" disks
                    found_unexpected_disks = False
                    for vm_disk in vm.storage_profile.data_disks:
                        state_disk_id = next((_d_id for _d_id, _disk in self.block_device_mapping.iteritems()
                                              if vm_disk.virtual_hard_disk.uri == _disk['media_link']), None)
                        if state_disk_id is None:
                            self.warn("unexpected disk {0}({1}) is attached to this virtual machine"
                                      .format(vm_disk.name, vm_disk.virtual_hard_disk.uri))
                            vm.storage_profile.data_disks.remove(vm_disk)
                            found_unexpected_disks = True
                    if found_unexpected_disks:
                        self.log("detaching unexpected disk(s)...")
                        self.cmc().virtual_machines.create_or_update(self.resource_group, vm)

                else:
                    self.warn_not_supposed_to_exist(valuable_data = True)
                    self.confirm_destroy()
            else:
                if self.vm_id:
                    self.warn("the instance seems to have been destroyed behind our back")
                    if not allow_recreate: raise Exception("use --allow-recreate to fix")
                    self._node_deleted()

        if self.vm_id and not allow_reboot:
            if defn.size != self.size:
                raise Exception("reboot is required to change the virtual machine size; please run with --allow-reboot")
            if defn.availability_set != self.availability_set:
                raise Exception("reboot is required to change the availability set name; please run with --allow-reboot")

        self._assert_no_impossible_disk_changes(defn)

        # change the root disk of a deployed vm
        # this is not directly supported by create_or_update API
        if self.vm_id:
            def_root_disk_id = defn_find_root_disk(defn.block_device_mapping)
            assert def_root_disk_id is not None
            def_root_disk = defn.block_device_mapping[def_root_disk_id]
            state_root_disk_id = find_root_disk(self.block_device_mapping)
            assert state_root_disk_id is not None
            state_root_disk = self.block_device_mapping[state_root_disk_id]

            if ( (def_root_disk_id != state_root_disk_id) or
                 (def_root_disk['host_caching'] != state_root_disk['host_caching']) or
                 (def_root_disk['name'] != state_root_disk['name']) ):
                self.warn("a modification of the root disk is requested "
                          "that requires that the virtual machine is re-created")
                if allow_recreate:
                    self.log("destroying the virtual machine, but preserving the disk contents...")
                    self.destroy_resource()
                    self._node_deleted()
                else:
                    raise Exception("use --allow-recreate to fix")

        self._change_existing_disk_parameters(defn)

        self._create_vm(defn)

        self._create_missing_attach_detached(defn)

        self._generate_default_encryption_keys()

        if self.properties_changed(defn):
            self.log("updating properties of {0}...".format(self.full_name))
            vm = self.get_vm_assert_exists()
            vm.hardware_profile = HardwareProfile(virtual_machine_size = defn.size)
            self.cmc().virtual_machines.create_or_update(self.resource_group, vm)
            self.copy_properties(defn)


    # change existing disk parameters as much as possible within the technical limitations
    def _change_existing_disk_parameters(self, defn):
        for d_id, disk in defn.block_device_mapping.iteritems():
            state_disk = self.block_device_mapping.get(d_id, None)
            if state_disk is None: continue
            lun = device_name_to_lun(disk['device'])
            if lun is None: continue
            if self.vm_id and not state_disk.get('needs_attach', False):
                if disk['host_caching'] != state_disk['host_caching']:
                    self.log("changing parameters of the attached disk {0}({1})"
                             .format(disk['name'], d_id))
                    vm = self.get_vm_assert_exists()
                    vm_disk = next((_disk for _disk in vm.storage_profile.data_disks
                                         if _disk.virtual_hard_disk.uri == disk['media_link']), None)
                    if vm_disk is None:
                        raise Exception("disk {0}({1}) was supposed to be attached at {2} "
                                        "but wasn't found; please run deploy --check to fix this"
                                        .format(disk['name'], d_id, disk['device']))
                    vm_disk.caching = disk['host_caching']
                    self.cmc().virtual_machines.create_or_update(self.resource_group, vm)
                    state_disk['host_caching'] = disk['host_caching']
            else:
                state_disk['host_caching'] = disk['host_caching']
                state_disk['name'] = disk['name']
                state_disk['device'] = disk['device']
            state_disk['encrypt'] = disk['encrypt']
            state_disk['passphrase'] = disk['passphrase']
            state_disk['is_ephemeral'] = disk['is_ephemeral']
            self.update_block_device_mapping(d_id, state_disk)

    # Certain disk configuration changes can't be deployed in
    # one step such as replacing a disk attached to a particular
    # LUN or reattaching a disk at a different LUN.
    # You can reattach the os disk as a data disk in one step.
    # You can't reattach the data disk as os disk in one step.
    # This is a limitation of the disk modification process,
    # which ensures clean dismounts:
    # new disks are attached, nixos configuration is deployed
    # which mounts new disks and dismounts the disks about to
    # be detached, and only then the disks are detached.
    def _assert_no_impossible_disk_changes(self, defn):
        if self.vm_id is None: return

        for d_id, disk in defn.block_device_mapping.iteritems():
            same_lun_id = next((_id for _id, _d in self.block_device_mapping.iteritems()
                                    if _d['device'] == disk['device']), None)
            disk_lun = device_name_to_lun(disk['device'])
            if same_lun_id is not None and disk_lun is not None and (same_lun_id != d_id) and (
                  not self.block_device_mapping[same_lun_id].get("needs_attach", False) ):
                raise Exception("can't attach Azure disk {0}({1}) because the target LUN {2} is already "
                                "occupied by Azure disk {3}; you need to deploy a configuration "
                                "with this LUN left empty before using it to attach a different data disk"
                                .format(disk['name'], disk["media_link"], disk["device"], same_lun_id))

            state_disk = self.block_device_mapping.get(d_id, None)
            _lun = state_disk and device_name_to_lun(state_disk['device'])
            if state_disk and _lun is not None and not state_disk.get('needs_attach', False):
                if state_disk['device'] != disk['device']:
                    raise Exception("can't reattach Azure disk {0}({1}) to a different LUN in one step; "
                                    "you need to deploy a configuration with this disk detached from {2} "
                                  "before attaching it to {3}"
                                 .format(disk['name'], d_id, state_disk['device'], disk['device']))
                if state_disk['name'] != disk['name']:
                    raise Exception("cannot change the name of the attached disk {0}({1})"
                                    .format(state_disk['name'], d_id))

    # create missing, attach detached disks
    def _create_missing_attach_detached(self, defn):
        for d_id, disk in defn.block_device_mapping.iteritems():
            lun = device_name_to_lun(disk['device'])
            if lun is None: continue
            _disk = self.block_device_mapping.get(d_id, None)
            if _disk and not _disk.get("needs_attach", False): continue

            self.log("attaching data disk {0}({1})".format(disk['name'], d_id))
            vm = self.get_vm_assert_exists()
            vm.storage_profile.data_disks.append(DataDisk(
                name = disk['name'],
                virtual_hard_disk = VirtualHardDisk(uri = disk['media_link']),
                caching = disk['host_caching'],
                create_option = ( DiskCreateOptionTypes.attach
                                  if self.blob_exists(disk['media_link'])
                                  else DiskCreateOptionTypes.empty ),
                lun = lun,
                disk_size_gb = disk['size']
            ))
            self.cmc().virtual_machines.create_or_update(self.resource_group, vm)
            self.update_block_device_mapping(d_id, disk)

    # generate LUKS key if the model didn't specify one
    def _generate_default_encryption_keys(self):
        for d_id, disk in self.block_device_mapping.iteritems():
            if disk.get('encrypt', False) and ( disk.get('passphrase', "") == ""
                                            and self.generated_encryption_keys.get(d_id, None) is None):
                self.log("generating an encryption key for disk {0}({1})"
                         .format(disk['name'], d_id))
                self.update_generated_encryption_keys(d_id, generate_random_string(length=256))


    def _create_vm(self, defn):
        if self.public_ip is None and defn.obtain_ip:
            self.log("getting an IP address")
            self.nrpc().public_ip_addresses.create_or_update(
                self.resource_group, self.machine_name,
                PublicIpAddress(
                    location = defn.location,
                    public_ip_allocation_method = 'Dynamic',
                    idle_timeout_in_minutes = 4,
                ))
            self.public_ip = self.machine_name
            self.obtain_ip = defn.obtain_ip

        if self.network_interface is None:
            self.log("creating a network interface")
            public_ip_id = self.nrpc().public_ip_addresses.get(
                               self.resource_group, self.public_ip).public_ip_address.id

            subnet = self.nrpc().subnets.get(self.resource_group, self.virtual_network, self.virtual_network).subnet
            self.nrpc().network_interfaces.create_or_update(
                self.resource_group, self.machine_name,
                NetworkInterface(name = self.machine_name,
                                 location = defn.location,
                                 ip_configurations = [ NetworkInterfaceIpConfiguration(
                                     name='default',
                                     private_ip_allocation_method = IpAllocationMethod.dynamic,
                                     subnet = subnet,
                                     public_ip_address = ResourceId(id = public_ip_id)
                                 )]
                                ))
            self.network_interface = self.machine_name

        if self.vm_id: return

        if self.get_settled_resource():
            raise Exception("tried creating a virtual machine that already exists; "
                            "please run 'deploy --check' to fix this")

        root_disk_id = defn_find_root_disk(defn.block_device_mapping)
        assert root_disk_id is not None
        root_disk_spec = defn.block_device_mapping[root_disk_id]
        existing_root_disk = self.block_device_mapping.get(root_disk_id, None)

        self.log("creating {0}...".format(self.full_name))
        nic_id = self.nrpc().network_interfaces.get(
                               self.resource_group, self.network_interface).network_interface.id
        custom_data = ('ssh_host_ecdsa_key=$(cat<<____HERE\n{0}\n____HERE\n)\n'
                       'ssh_host_ecdsa_key_pub="{1}"\nssh_root_auth_key="{2}"\n'
                      ).format(self.private_host_key, self.public_host_key, self.public_client_key)

        data_disks = [ DataDisk(
                           name = disk['name'],
                           virtual_hard_disk = VirtualHardDisk(uri = disk['media_link']),
                           caching = disk['host_caching'],
                           create_option = ( DiskCreateOptionTypes.attach
                                             if self.blob_exists(disk['media_link'])
                                             else DiskCreateOptionTypes.empty ),
                           lun = device_name_to_lun(disk['device']),
                           disk_size_gb = disk['size']
                           )
                       for disk_id, disk in defn.block_device_mapping.iteritems()
                       if device_name_to_lun(disk['device']) is not None ]

        root_disk_exists = self.blob_exists(root_disk_spec['media_link'])

        req = self.cmc().virtual_machines.begin_creating_or_updating(
            self.resource_group,
            VirtualMachine(
                location = self.location,
                name = self.machine_name,
                os_profile = ( None
                               if root_disk_exists
                               else OSProfile(
                                   admin_username="randomuser",
                                   admin_password="aA9+" + generate_random_string(length=32),
                                   computer_name=self.machine_name,
                                   custom_data = base64.b64encode(custom_data)
                             ) ),
                hardware_profile = HardwareProfile(virtual_machine_size = defn.size),
                network_profile = NetworkProfile(
                    network_interfaces = [
                        NetworkInterfaceReference(reference_uri = nic_id)
                    ],
                ),
                storage_profile = StorageProfile(
                    os_disk = OSDisk(
                        caching = root_disk_spec['host_caching'],
                        create_option = ( DiskCreateOptionTypes.attach
                                          if root_disk_exists
                                          else DiskCreateOptionTypes.from_image),
                        name = root_disk_spec['name'],
                        virtual_hard_disk = VirtualHardDisk(uri = root_disk_spec['media_link']),
                        source_image = (None
                                        if root_disk_exists
                                        else VirtualHardDisk(uri = defn.root_disk_image_url) ),
                        operating_system_type = "Linux"
                    ),
                    data_disks = data_disks
                )
            )
        )
        print req.__dict__

        # we take a shortcut: wait for either provisioning to fail or for public ip to get assigned
        def check_req():
            return ((self.fetch_public_ip() is not None)
                 or (self.cmc().get_long_running_operation_status(req.azure_async_operation).status
                        != ComputeOperationStatus.in_progress))
        check_wait(check_req, initial=1, max_tries=500, exception=True)

        req_status = self.cmc().get_long_running_operation_status(req.azure_async_operation)
        if req_status.status == ComputeOperationStatus.failed:
            raise Exception('failed to provision {0}; {1}'
                        .format(self.full_name, req_status.error.__dict__))

        self.vm_id = self.machine_name
        self.state = self.STARTING
        self.ssh_pinged = False
        self.copy_properties(defn)

        self.public_ipv4 = self.fetch_public_ip()
        self.log("got IP: {0}".format(self.public_ipv4))
        self.update_ssh_known_hosts()

        for d_id, disk in defn.block_device_mapping.iteritems():
            self.update_block_device_mapping(d_id, disk)


    def after_activation(self, defn):
        # detach the volumes that are no longer in the deployment spec
        for d_id, disk in self.block_device_mapping.items():
            lun = device_name_to_lun(disk['device'])
            if d_id not in defn.block_device_mapping:

                if not disk.get('needs_attach', False) and lun is not None:
                    if disk.get('encrypt', False):
                        dm = "/dev/mapper/{0}".format(disk['name'])
                        self.log("unmounting device '{0}'...".format(dm))
                        # umount with -l flag in case if the regular umount run by activation failed
                        self.run_command("umount -l {0}".format(dm), check=False)
                        self.run_command("cryptsetup luksClose {0}".format(dm), check=False)
                    else:
                        self.log("unmounting device '{0}'...".format(disk['device']))
                        self.run_command("umount -l {0}".format(disk['device']), check=False)

                    self.log("detaching Azure disk {0}({1})...".format(disk['name'], d_id))
                    vm = self.get_vm_assert_exists()
                    vm.storage_profile.data_disks = [
                        _disk
                        for _disk in vm.storage_profile.data_disks
                        if _disk.virtual_hard_disk.uri != disk['media_link'] ]
                    self.cmc().virtual_machines.create_or_update(self.resource_group, vm)
                    disk['needs_attach'] = True
                    self.update_block_device_mapping(d_id, disk)

                if disk['is_ephemeral']:
                    self._delete_volume(disk['media_link'], disk_name = disk['name'])

                # rescan the disk device, to make its device node disappear on older kernels
                self.run_command("sg_scan {0}".format(disk['device']), check=False)

                self.update_block_device_mapping(d_id, None)
                self._delete_encryption_key(d_id)


    def reboot(self, hard=False):
        if hard:
            self.log("sending hard reset to Azure machine...")
            self.cmc().virtual_machines.restart(self.resource_group, self.machine_name)
            self.state = self.STARTING
            self.ssh.reset()
        else:
            MachineState.reboot(self, hard=hard)
        self.ssh_pinged = False

    def start(self):
        if self.vm_id:
            self.state = self.STARTING
            self.log("starting Azure machine...")
            self.cmc().virtual_machines.start(self.resource_group, self.machine_name)
            self.wait_for_ssh(check=True)
            self.send_keys()

    def stop(self):
        if self.vm_id:
           #FIXME: there's also "stopped deallocated" version of this. how to integrate?
            self.log("stopping Azure machine... ")
            self.state = self.STOPPING
            self.cmc().virtual_machines.power_off(self.resource_group, self.machine_name)
            self.state = self.STOPPED
            self.ssh.reset()
            self.ssh_pinged = False

    def destroy(self, wipe=False):
        if wipe:
            log.warn("wipe is not supported")

        if self.vm_id:
            vm = self.get_resource()
            if vm:
                question = "are you sure you want to destroy {0}?"
                if not self.depl.logger.confirm(question.format(self.full_name)):
                    return False
                self.log("destroying the Azure machine...")
                self.destroy_resource()
            else:
                self.warn("seems to have been destroyed already")
        self._node_deleted()

        # Destroy volumes created for this instance.
        for d_id, disk in self.block_device_mapping.items():
            if disk['is_ephemeral']:
                self._delete_volume(disk['media_link'], disk_name = disk['name'])
            self.update_block_device_mapping(d_id, None)
            self._delete_encryption_key(d_id)

        if self.network_interface:
            self.log("destroying the network interface...")
            try:
                self.nrpc().network_interfaces.get(self.resource_group, self.network_interface)
                self.nrpc().network_interfaces.delete(self.resource_group, self.network_interface)
            except azure.common.AzureMissingResourceHttpError:
                self.warn("seems to have been destroyed already")
            self.network_interface = None

        if self.public_ip:
            self.log("releasing the ip address...")
            try:
                self.nrpc().public_ip_addresses.get(self.resource_group, self.public_ip)
                self.nrpc().public_ip_addresses.delete(self.resource_group, self.public_ip)
            except azure.common.AzureMissingResourceHttpError:
                self.warn("seems to have been released already")
            self.public_ip = None
            self.obtain_ip = None

        if self.generated_encryption_keys != {}:
            if not self.depl.logger.confirm("{0} resource still stores generated encryption keys for disks {1}; "
                                            "if the resource is deleted, the keys are deleted along with it "
                                            "and the data will be lost even if you have a copy of the disks' "
                                            "contents; are you sure you want to delete the encryption keys?"
                                            .format(self.full_name, self.generated_encryption_keys.keys()) ):
                raise Exception("cannot continue")
        return True


    def backup(self, defn, backup_id):
        self.log("backing up {0} using ID '{1}'".format(self.full_name, backup_id))

        if sorted(defn.block_device_mapping.keys()) != sorted(self.block_device_mapping.keys()):
            self.warn("the list of disks currently deployed doesn't match the current deployment"
                     " specification; consider running 'deploy' first; the backup may be incomplete")

        backup = {}
        _backups = self.backups
        for d_id, disk in self.block_device_mapping.iteritems():
            media_link = disk['media_link']
            self.log("snapshotting the BLOB {0} backing the Azure disk {1}"
                     .format(media_link, disk['name']))
            blob = parse_blob_url(media_link)
            if blob is None:
                raise Exception("failed to parse BLOB URL {0}"
                                .format(media_link))
            if blob['storage'] != self.storage:
                raise Exception("storage {0} provided in the deployment specification "
                                "doesn't match the storage of BLOB {1}"
                                .format(self.storage, media_link))
            snapshot = self.bs().snapshot_blob(blob['container'], blob['name'],
                                               x_ms_meta_name_values = {
                                                   'nixops_backup_id': backup_id,
                                                   'description': "backup of disk {0} attached to {1}"
                                                                  .format(disk['name'], self.machine_name)
                                               })
            backup[media_link] = snapshot['x-ms-snapshot']
            _backups[backup_id] = backup
            self.backups = _backups

    def restore(self, defn, backup_id, devices=[]):
        self.log("restoring {0} to backup '{1}'".format(self.full_name, backup_id))

        if self.vm_id:
            self.stop()
            self.log("temporarily deprovisioning {0}".format(self.full_name))
            self.destroy_resource()
            self._node_deleted()

        for d_id, disk in self.block_device_mapping.items():
            media_link = disk['media_link']
            s_id = self.backups[backup_id].get(media_link, None)
            if s_id and (devices == [] or media_link in devices or
                         disk['name'] in devices or disk['device'] in devices):
                blob = parse_blob_url(media_link)
                if blob is None:
                    self.warn("failed to parse BLOB URL {0}; skipping"
                              .format(media_link))
                    continue
                if blob["storage"] != self.storage:
                    raise Exception("storage {0} provided in the deployment specification "
                                    "doesn't match the storage of BLOB {1}"
                                    .format(self.storage, media_link))
                try:
                    self.bs().get_blob_properties(
                            blob["container"], "{0}?snapshot={1}"
                                                .format(blob["name"], s_id))
                except azure.common.AzureMissingResourceHttpError:
                    self.warn("snapshot {0} for disk {1} is missing; skipping".format(s_id, d_id))
                    continue

                self.log("restoring BLOB {0} from snapshot"
                         .format(media_link, s_id))
                self.bs().copy_blob(blob["container"], blob["name"],
                                   "{0}?snapshot={1}"
                                   .format(media_link, s_id) )

        # restore the currently deployed configuration(defn = self)
        self._create_vm(self)


    def remove_backup(self, backup_id, keep_physical=False):
        self.log('removing backup {0}'.format(backup_id))
        _backups = self.backups

        if not backup_id in _backups.keys():
            self.warn('backup {0} not found; skipping'.format(backup_id))
        else:
            for blob_url, snapshot_id in _backups[backup_id].iteritems():
                try:
                    self.log('removing snapshot {0} of BLOB {1}'.format(snapshot_id, blob_url))
                    blob = parse_blob_url(blob_url)
                    if blob is None:
                        self.warn("failed to parse BLOB URL {0}; skipping".format(blob_url))
                        continue
                    if blob["storage"] != self.storage:
                        raise Exception("storage {0} provided in the deployment specification "
                                        "doesn't match the storage of BLOB {1}"
                                        .format(self.storage, blob_url))

                    self.bs().delete_blob(blob["container"], blob["name"], snapshot_id)
                except azure.common.AzureMissingResourceHttpError:
                    self.warn('snapshot {0} of BLOB {1} does not exist; skipping'
                              .format(snapshot_id, blob_url))

            _backups.pop(backup_id)
            self.backups = _backups

    def get_backups(self):
        backups = {}
        for b_id, snapshots in self.backups.iteritems():
            backups[b_id] = {}
            backup_status = "complete"
            info = []
            processed = set()
            for d_id, disk in self.block_device_mapping.items():
                media_link = disk['media_link']
                if not media_link in snapshots.keys():
                    backup_status = "incomplete"
                    info.append("{0} - {1} - not available in backup"
                                .format(self.name, d_id))
                else:
                    snapshot_id = snapshots[media_link]
                    processed.add(media_link)
                    blob = parse_blob_url(media_link)
                    if blob is None:
                        info.append("failed to parse BLOB URL {0}"
                                    .format(media_link))
                        backup_status = "unavailable"
                    elif blob["storage"] != self.storage:
                        info.append("storage {0} provided in the deployment specification "
                                    "doesn't match the storage of BLOB {1}"
                                    .format(self.storage, media_link))
                        backup_status = "unavailable"
                    else:
                        try:
                            snapshot = self.bs().get_blob_properties(
                                            blob["container"], "{0}?snapshot={1}"
                                                               .format(blob["name"], snapshot_id))
                        except azure.common.AzureMissingResourceHttpError:
                            info.append("{0} - {1} - {2} - snapshot has disappeared"
                                        .format(self.name, d_id, snapshot_id))
                            backup_status = "unavailable"

            for media_link in (set(snapshots.keys())-processed):
                info.append("{0} - {1} - {2} - a snapshot of a disk that is not or no longer deployed"
                            .format(self.name, media_link, snapshots[media_link]))
            backups[b_id]['status'] = backup_status
            backups[b_id]['info'] = info

        return backups


    def _check(self, res):
        if(self.subscription_id is None or self.authority_url is None or
           self.user is None or self.password is None):
            res.exists = False
            res.is_up = False
            self.state = self.MISSING;
            return

        vm = self.get_resource()
        if vm is None:
            res.exists = False
            res.is_up = False
            self.state = self.MISSING;
        else:
            res.exists = True

            res.is_up = vm.provisioning_state == ProvisioningStateTypes.succeeded
            if vm.provisioning_state == ProvisioningStateTypes.failed:
                res.messages.append("vm resource exists, but is in a failed state")
            if not res.is_up: self.state = self.STOPPED
            if res.is_up:
                # check that all disks are attached
                res.disks_ok = True
                for d_id, disk in self.block_device_mapping.iteritems():
                    if device_name_to_lun(disk['device']) is None:
                        if vm.storage_profile.os_disk.virtual_hard_disk.uri != disk['media_link']:
                            res.disks_ok = False
                            res.messages.append("different root disk instead of {0}".format(d_id))
                        else: continue
                    if all(disk['media_link'] != d.virtual_hard_disk.uri
                           for d in vm.storage_profile.data_disks):
                        res.disks_ok = False
                        res.messages.append("disk {0}({1}) is detached".format(disk['name'], d_id))
                        if not self.blob_exists(disk['media_link']):
                            res.messages.append("disk {0}({1}) is destroyed".format(disk['name'], d_id))

                self.handle_changed_property('public_ipv4', self.fetch_public_ip())
                self.update_ssh_known_hosts()

                MachineState._check(self, res)

    def get_physical_spec(self):
        block_device_mapping = {
            disk["device"] : {
                'passphrase': Call(RawValue("pkgs.lib.mkOverride 10"),
                                   self.generated_encryption_keys[d_id])
            }
            for d_id, disk in self.block_device_mapping.items()
            if (disk.get('encrypt', False)
                and disk.get('passphrase', "") == ""
                and self.generated_encryption_keys.get(d_id, None) is not None)
        }
        return {
            'require': [
                RawValue("<nixpkgs/nixos/modules/virtualisation/azure-common.nix>")
            ],
            ('deployment', 'azure', 'blockDeviceMapping'): block_device_mapping,
        }

    def get_keys(self):
        keys = MachineState.get_keys(self)
        # Ugly: we have to add the generated keys because they're not
        # there in the first evaluation (though they are present in
        # the final nix-build).
        for d_id, disk in self.block_device_mapping.items():
            if disk.get('encrypt', False) and ( disk.get('passphrase', "") == ""
                                            and self.generated_encryption_keys.get(d_id, None) is not None):
                key_name = disk['name']
                keys["luks-" + key_name] = {
                    'text': self.generated_encryption_keys[d_id],
                    'group': 'root',
                    'permissions': '0600',
                    'user': 'root'
                }
        return keys


    def create_after(self, resources, defn):
        from nixops.resources.azure_blob import AzureBLOBState
        from nixops.resources.azure_blob_container import AzureBLOBContainerState
        from nixops.resources.azure_storage import AzureStorageState
        from nixops.resources.azure_resource_group import AzureResourceGroupState
        from nixops.resources.azure_virtual_network import AzureVirtualNetworkState

        return {r for r in resources
                  if isinstance(r, AzureBLOBContainerState) or isinstance(r, AzureStorageState) or
                     isinstance(r, AzureBLOBState) or isinstance(r, AzureResourceGroupState) or
                     isinstance(r, AzureVirtualNetworkState)}

    # return ssh host and port formatted for ssh/known_hosts file
    def get_ssh_host_port(self):
        return self.public_ipv4

    @MachineState.ssh_port.getter
    def ssh_port(self):
        if self.public_ipv4:
            return super(AzureState, self).ssh_port
        return None

    def update_ssh_known_hosts(self):
        ssh_host_port = self.get_ssh_host_port()
        if ssh_host_port:
            known_hosts.add(ssh_host_port, self.public_host_key)

    def get_ssh_name(self):
        ip = self.public_ipv4
        if ip is None:
            raise Exception("{0} does not have a public IPv4 address and is not reachable "
                            .format(self.full_name))
        return ip

    def get_ssh_private_key_file(self):
        return self._ssh_private_key_file or self.write_ssh_private_key(self.private_client_key)

    def get_ssh_flags(self, scp=False):
        return [ "-i", self.get_ssh_private_key_file() ] + super(AzureState, self).get_ssh_flags(scp = scp)