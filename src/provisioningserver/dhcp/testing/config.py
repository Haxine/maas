# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test helpers related to DHCP configuration."""

__all__ = [
    'make_subnet_config',
    ]

import random

from maastesting.factory import factory
from netaddr import IPAddress


def make_subnet_pool(
        network, start_ip=None, end_ip=None, failover_peer=None):
    """Return a pool entry for a subnet from network."""
    if start_ip is None and end_ip is None:
        start_ip, end_ip = factory.make_ip_range(network)
    if failover_peer is None:
        failover_peer = factory.make_name("failover")
    return {
        "ip_range_low": str(start_ip),
        "ip_range_high": str(end_ip),
        "failover_peer": failover_peer,
    }


def make_dhcp_snippets(allow_empty=True):
    # DHCP snippets are optional
    if allow_empty and factory.pick_bool():
        return []
    return [{
        'name': factory.make_name('name'),
        'description': factory.make_name('description'),
        'value': factory.make_name('value'),
        } for _ in range(3)]


def make_host(
        hostname=None, interface_name=None,
        mac_address=None, ip=None, ipv6=False, dhcp_snippets=None):
    """Return a host entry for a subnet from network."""
    if hostname is None:
        hostname = factory.make_name("host")
    if interface_name is None:
        interface_name = factory.make_name("eth")
    if mac_address is None:
        mac_address = factory.make_mac_address()
    if ip is None:
        if ipv6 is True:
            ip = str(factory.make_ipv6_address())
        else:
            ip = str(factory.make_ipv4_address())
    if dhcp_snippets is None:
        dhcp_snippets = make_dhcp_snippets()
    return {
        "host": "%s-%s" % (hostname, interface_name),
        "mac": mac_address,
        "ip": ip,
        "dhcp_snippets": dhcp_snippets,
    }


def make_subnet_config(network=None, pools=None, ipv6=False,
                       dhcp_snippets=None):
    """Return complete DHCP configuration dict for a subnet."""
    if network is None:
        if ipv6 is True:
            network = factory.make_ipv6_network()
        else:
            network = factory.make_ipv4_network()
    if pools is None:
        pools = [make_subnet_pool(network)]
    if dhcp_snippets is None:
        dhcp_snippets = make_dhcp_snippets()
    return {
        'subnet': str(IPAddress(network.first)),
        'subnet_mask': str(network.netmask),
        'subnet_cidr': str(network.cidr),
        'broadcast_ip': str(network.broadcast),
        'dns_servers': str(factory.pick_ip_in_network(network)),
        'ntp_server': str(factory.pick_ip_in_network(network)),
        'domain_name': '%s.example.com' % factory.make_name('domain'),
        'router_ip': str(factory.pick_ip_in_network(network)),
        'pools': pools,
        'dhcp_snippets': dhcp_snippets,
        }


def make_shared_network(name=None, subnets=None, ipv6=False):
    """Return complete DHCP configuration dict for a shared network."""
    if name is None:
        name = factory.make_name("vlan")
    if subnets is None:
        subnets = [
            make_subnet_config(ipv6=ipv6)
            for _ in range(3)
        ]
    return {
        "name": name,
        "subnets": subnets,
    }


def make_failover_peer_config(
        name=None, mode=None, address=None, peer_address=None):
    """Return complete DHCP configuration dict for a failover peer."""
    if name is None:
        name = factory.make_name("failover")
    if mode is None:
        mode = random.choice(["primary", "secondary"])
    if address is None:
        address = factory.make_ip_address()
    if peer_address is None:
        peer_address = factory.make_ip_address()
    return {
        'name': name,
        'mode': mode,
        'address': address,
        'peer_address': peer_address,
        }


def make_interface(name=None):
    if name is None:
        name = factory.make_name("eth")
    return {
        'name': name,
    }