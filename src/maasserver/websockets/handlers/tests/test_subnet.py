# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.websockets.handlers.subnet`"""

__all__ = []

from maasserver.models.subnet import Subnet
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import reload_object
from maasserver.websockets.handlers.subnet import SubnetHandler
from maasserver.websockets.handlers.timestampedmodel import dehydrate_datetime
from netaddr import IPNetwork
from provisioningserver.utils.network import IPRangeStatistics
from testtools import ExpectedException
from testtools.matchers import Equals


class TestSubnetHandler(MAASServerTestCase):

    def dehydrate_subnet(self, subnet, for_list=False):
        data = {
            "id": subnet.id,
            "updated": dehydrate_datetime(subnet.updated),
            "created": dehydrate_datetime(subnet.created),
            "name": subnet.name,
            "description": subnet.description,
            "dns_servers": (
                " ".join(sorted(subnet.dns_servers))
                if subnet.dns_servers is not None else ''
            ),
            "vlan": subnet.vlan_id,
            "space": subnet.space_id,
            "rdns_mode": subnet.rdns_mode,
            "allow_proxy": subnet.allow_proxy,
            "cidr": subnet.cidr,
            "gateway_ip": subnet.gateway_ip,
        }
        full_range = subnet.get_iprange_usage()
        metadata = IPRangeStatistics(full_range)
        data['statistics'] = metadata.render_json(
            include_ranges=True, include_suggestions=True)
        data['version'] = IPNetwork(subnet.cidr).version
        if not for_list:
            data["ip_addresses"] = subnet.render_json_for_related_ips(
                with_username=True, with_node_summary=True)
        return data

    def test_get(self):
        user = factory.make_User()
        handler = SubnetHandler(user, {})
        subnet = factory.make_Subnet()
        expected_data = self.dehydrate_subnet(subnet)
        result = handler.get({"id": subnet.id})
        self.assertThat(result, Equals(expected_data))

    def test_get_handles_null_dns_servers(self):
        user = factory.make_User()
        handler = SubnetHandler(user, {})
        subnet = factory.make_Subnet()
        subnet.dns_servers = None
        subnet.save()
        expected_data = self.dehydrate_subnet(subnet)
        result = handler.get({"id": subnet.id})
        self.assertThat(result, Equals(expected_data))

    def test_list(self):
        user = factory.make_User()
        handler = SubnetHandler(user, {})
        factory.make_Subnet()
        expected_subnets = [
            self.dehydrate_subnet(subnet, for_list=True)
            for subnet in Subnet.objects.all()
            ]
        self.assertItemsEqual(
            expected_subnets,
            handler.list({}))


class TestSubnetHandlerDelete(MAASServerTestCase):

    def test__delete_as_admin_success(self):
        user = factory.make_admin()
        handler = SubnetHandler(user, {})
        subnet = factory.make_Subnet()
        handler.delete({
            "id": subnet.id,
        })
        subnet = reload_object(subnet)
        self.assertThat(subnet, Equals(None))

    def test__delete_as_non_admin_asserts(self):
        user = factory.make_User()
        handler = SubnetHandler(user, {})
        subnet = factory.make_Subnet()
        with ExpectedException(AssertionError, "Permission denied."):
            handler.delete({
                "id": subnet.id,
            })

    def test__reloads_user(self):
        user = factory.make_admin()
        handler = SubnetHandler(user, {})
        subnet = factory.make_Subnet()
        user.is_superuser = False
        user.save()
        with ExpectedException(AssertionError, "Permission denied."):
            handler.delete({
                "id": subnet.id,
            })