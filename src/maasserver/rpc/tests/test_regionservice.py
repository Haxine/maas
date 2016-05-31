# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the region's RPC implementation."""

__all__ = []

from collections import defaultdict
from datetime import timedelta
from operator import attrgetter
import os.path
import random
from random import randint
from socket import gethostname
import threading
from unittest import skip
from unittest.mock import (
    ANY,
    call,
    MagicMock,
    Mock,
    sentinel,
)

from crochet import wait_for
from django.db import IntegrityError
from maasserver import (
    eventloop,
    locks,
)
from maasserver.enum import (
    NODE_TYPE,
    SERVICE_STATUS,
)
from maasserver.models import (
    RackController,
    RegionController,
    RegionControllerProcess,
    RegionRackRPCConnection,
    Service as ServiceModel,
    timestampedmodel,
)
from maasserver.models.timestampedmodel import now
from maasserver.rpc import regionservice
from maasserver.rpc.regionservice import (
    getRegionID,
    Region,
    RegionAdvertising,
    RegionAdvertisingService,
    RegionServer,
    RegionService,
    registerConnection,
    unregisterConnection,
)
from maasserver.rpc.testing.doubles import HandshakingRegionServer
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import (
    reload_object,
    transactional,
)
from maasserver.utils.threads import deferToDatabase
from maastesting.matchers import (
    IsFiredDeferred,
    IsUnfiredDeferred,
    MockAnyCall,
    MockCalledOnce,
    MockCalledOnceWith,
    MockCallsMatch,
    Provides,
)
from maastesting.runtest import MAASCrochetRunTest
from maastesting.testcase import MAASTestCase
from maastesting.twisted import (
    always_fail_with,
    always_succeed_with,
    extract_result,
    TwistedLoggerFixture,
)
import netaddr
from provisioningserver.rpc import (
    common,
    exceptions,
)
from provisioningserver.rpc.exceptions import CannotRegisterRackController
from provisioningserver.rpc.interfaces import IConnection
from provisioningserver.rpc.region import RegisterRackController
from provisioningserver.rpc.testing import call_responder
from provisioningserver.rpc.testing.doubles import DummyConnection
from provisioningserver.utils import events
from provisioningserver.utils.twisted import (
    callInReactorWithTimeout,
    DeferredValue,
)
from testtools.deferredruntest import assert_fails_with
from testtools.matchers import (
    AfterPreprocessing,
    AllMatch,
    Equals,
    HasLength,
    Is,
    IsInstance,
    MatchesAll,
    MatchesListwise,
    MatchesStructure,
    Not,
)
from twisted.application.service import Service
from twisted.internet import (
    reactor,
    tcp,
)
from twisted.internet.address import IPv4Address
from twisted.internet.defer import (
    CancelledError,
    Deferred,
    DeferredList,
    fail,
    inlineCallbacks,
    succeed,
)
from twisted.internet.endpoints import TCP4ServerEndpoint
from twisted.internet.error import ConnectionClosed
from twisted.internet.interfaces import IStreamServerEndpoint
from twisted.internet.protocol import Factory
from twisted.internet.task import LoopingCall
from twisted.logger import globalLogPublisher
from twisted.protocols import amp
from twisted.python import log
from twisted.python.failure import Failure
from twisted.python.reflect import fullyQualifiedName
from zope.interface.verify import verifyObject


wait_for_reactor = wait_for(30)  # 30 seconds.


class TestRegisterAndUnregisterConnection(MAASServerTestCase):
    """Tests for the `registerConnection` and `unregisterConnection`
    function."""

    def test__adds_connection_and_removes_connection(self):
        region = factory.make_RegionController()
        process = factory.make_RegionControllerProcess(region=region)
        endpoint = factory.make_RegionControllerProcessEndpoint(process)

        self.patch(os, "getpid").return_value = process.pid

        host = MagicMock()
        host.host = endpoint.address
        host.port = endpoint.port

        rack_controller = factory.make_RackController()

        registerConnection(region.system_id, rack_controller, host)
        self.assertIsNotNone(
            RegionRackRPCConnection.objects.filter(
                endpoint=endpoint, rack_controller=rack_controller).first())

        # Checks that an exception is not raised if already registered.
        registerConnection(region.system_id, rack_controller, host)

        unregisterConnection(region.system_id, rack_controller.system_id, host)
        self.assertIsNone(
            RegionRackRPCConnection.objects.filter(
                endpoint=endpoint, rack_controller=rack_controller).first())


class TestGetRegionID(MAASTestCase):
    """Tests for `getRegionID`."""

    def test__getRegionID_fails_when_advertising_service_not_running(self):
        region_id = getRegionID()
        self.assertThat(region_id, IsFiredDeferred())
        error = self.assertRaises(KeyError, extract_result, region_id)
        self.assertThat(str(error), Equals(repr('rpc-advertise')))

    def test__getRegionID_returns_the_region_ID_when_available(self):
        service = RegionAdvertisingService()
        service.setName("rpc-advertise")
        service.setServiceParent(eventloop.services)
        self.addCleanup(service.disownServiceParent)
        region_id = getRegionID()
        self.assertThat(region_id, IsUnfiredDeferred())
        service.advertising.set(
            RegionAdvertising(sentinel.region_id, sentinel.process_id))
        self.assertThat(region_id, IsFiredDeferred())
        self.assertThat(region_id.result, Is(sentinel.region_id))


class TestRegionServer(MAASTransactionServerTestCase):

    def test_interfaces(self):
        protocol = RegionServer()
        # transport.getHandle() is used by AMP._getPeerCertificate, which we
        # call indirectly via the peerCertificate attribute in IConnection.
        self.patch(protocol, "transport")
        verifyObject(IConnection, protocol)

    def test_connectionMade_does_not_update_services_connection_set(self):
        service = RegionService()
        service.running = True  # Pretend it's running.
        service.factory.protocol = HandshakingRegionServer
        protocol = service.factory.buildProtocol(addr=None)  # addr is unused.
        self.assertDictEqual({}, service.connections)
        protocol.connectionMade()
        self.assertDictEqual({}, service.connections)

    def test_connectionMade_drops_connection_if_service_not_running(self):
        service = RegionService()
        service.running = False  # Pretend it's not running.
        service.factory.protocol = HandshakingRegionServer
        protocol = service.factory.buildProtocol(addr=None)  # addr is unused.
        transport = self.patch(protocol, "transport")
        self.assertDictEqual({}, service.connections)
        protocol.connectionMade()
        # The protocol is not added to the connection set.
        self.assertDictEqual({}, service.connections)
        # The transport is instructed to lose the connection.
        self.assertThat(transport.loseConnection, MockCalledOnceWith())

    def test_connectionLost_updates_services_connection_set(self):
        service = RegionService()
        service.running = True  # Pretend it's running.
        service.factory.protocol = HandshakingRegionServer
        protocol = service.factory.buildProtocol(addr=None)  # addr is unused.
        protocol.ident = factory.make_name("node")
        connectionLost_up_call = self.patch(amp.AMP, "connectionLost")
        service.connections[protocol.ident] = {protocol}

        protocol.connectionLost(reason=None)
        # The connection is removed from the set, but the key remains.
        self.assertDictEqual({protocol.ident: set()}, service.connections)
        # connectionLost() is called on the superclass.
        self.assertThat(connectionLost_up_call, MockCalledOnceWith(None))

    def test_connectionLost_calls_unregisterConnection_in_thread(self):
        service = RegionService()
        service.running = True  # Pretend it's running.
        service.factory.protocol = HandshakingRegionServer
        protocol = service.factory.buildProtocol(addr=None)  # addr is unused.
        protocol.ident = factory.make_name("node")
        protocol.host = sentinel.host
        protocol.hostIsRemote = True
        getRegionID = self.patch_autospec(regionservice, "getRegionID")
        getRegionID.return_value = succeed(sentinel.region_id)
        connectionLost_up_call = self.patch(amp.AMP, "connectionLost")
        service.connections[protocol.ident] = {protocol}

        mock_deferToDatabase = self.patch(regionservice, "deferToDatabase")
        protocol.connectionLost(reason=None)
        self.assertThat(
            mock_deferToDatabase, MockCalledOnceWith(
                unregisterConnection, sentinel.region_id, protocol.ident,
                protocol.host))
        # The connection is removed from the set, but the key remains.
        self.assertDictEqual({protocol.ident: set()}, service.connections)
        # connectionLost() is called on the superclass.
        self.assertThat(connectionLost_up_call, MockCalledOnceWith(None))

    def patch_authenticate_for_failure(self, client):
        authenticate = self.patch_autospec(client, "authenticateCluster")
        authenticate.side_effect = always_succeed_with(False)

    def patch_authenticate_for_error(self, client, exception):
        authenticate = self.patch_autospec(client, "authenticateCluster")
        authenticate.side_effect = always_fail_with(exception)

    def test_connectionMade_drops_connections_if_authentication_fails(self):
        service = RegionService()
        service.running = True  # Pretend it's running.
        service.factory.protocol = HandshakingRegionServer
        protocol = service.factory.buildProtocol(addr=None)  # addr is unused.
        self.patch_authenticate_for_failure(protocol)
        transport = self.patch(protocol, "transport")
        self.assertDictEqual({}, service.connections)
        protocol.connectionMade()
        # The protocol is not added to the connection set.
        self.assertDictEqual({}, service.connections)
        # The transport is instructed to lose the connection.
        self.assertThat(transport.loseConnection, MockCalledOnceWith())

    def test_connectionMade_drops_connections_if_authentication_errors(self):
        logger = self.useFixture(TwistedLoggerFixture())

        service = RegionService()
        service.running = True  # Pretend it's running.
        service.factory.protocol = HandshakingRegionServer
        protocol = service.factory.buildProtocol(addr=None)  # addr is unused.
        protocol.transport = MagicMock()
        exception_type = factory.make_exception_type()
        self.patch_authenticate_for_error(protocol, exception_type())
        self.assertDictEqual({}, service.connections)

        connectionMade = wait_for_reactor(protocol.connectionMade)
        connectionMade()

        # The protocol is not added to the connection set.
        self.assertDictEqual({}, service.connections)
        # The transport is instructed to lose the connection.
        self.assertThat(
            protocol.transport.loseConnection, MockCalledOnceWith())

        # The log was written to.
        self.assertDocTestMatches(
            """\
            Rack controller '...' could not be authenticated; dropping
            connection.
            Traceback (most recent call last):...
            """,
            logger.dump())

    def test_handshakeFailed_does_not_log_when_connection_is_closed(self):
        server = RegionServer()
        with TwistedLoggerFixture() as logger:
            server.handshakeFailed(Failure(ConnectionClosed()))
        # Nothing was logged.
        self.assertEqual("", logger.output)

    def make_handshaking_server(self):
        service = RegionService()
        service.running = True  # Pretend it's running.
        service.factory.protocol = HandshakingRegionServer
        return service.factory.buildProtocol(addr=None)  # addr is unused.

    def make_running_server(self):
        service = RegionService()
        service.running = True  # Pretend it's running.
        # service.factory.protocol = RegionServer
        return service.factory.buildProtocol(addr=None)  # addr is unused.

    def test_authenticateCluster_accepts_matching_digests(self):
        server = self.make_running_server()

        def calculate_digest(_, message):
            # Use the region's own authentication responder.
            return Region().authenticate(message)

        callRemote = self.patch_autospec(server, "callRemote")
        callRemote.side_effect = calculate_digest

        d = server.authenticateCluster()
        self.assertTrue(extract_result(d))

    def test_authenticateCluster_rejects_non_matching_digests(self):
        server = self.make_running_server()

        def calculate_digest(_, message):
            # Return some nonsense.
            response = {
                "digest": factory.make_bytes(),
                "salt": factory.make_bytes(),
            }
            return succeed(response)

        callRemote = self.patch_autospec(server, "callRemote")
        callRemote.side_effect = calculate_digest

        d = server.authenticateCluster()
        self.assertFalse(extract_result(d))

    def test_authenticateCluster_propagates_errors(self):
        server = self.make_running_server()
        exception_type = factory.make_exception_type()

        callRemote = self.patch_autospec(server, "callRemote")
        callRemote.return_value = fail(exception_type())

        d = server.authenticateCluster()
        self.assertRaises(exception_type, extract_result, d)

    def make_Region(self):
        patched_region = RegionServer()
        patched_region.factory = Factory.forProtocol(RegionServer)
        patched_region.factory.service = RegionService()
        return patched_region

    def test_register_is_registered(self):
        protocol = RegionServer()
        responder = protocol.locateResponder(
            RegisterRackController.commandName)
        self.assertIsNotNone(responder)

    def installFakeRegionAdvertisingService(self):
        service = RegionAdvertisingService()
        service.setName("rpc-advertise")
        service.advertising.set(RegionAdvertising(
            region_id=factory.make_name("region-id"),
            process_id=randint(1000, 9999)))
        service.setServiceParent(eventloop.services)
        self.addCleanup(service.disownServiceParent)

    @wait_for_reactor
    @inlineCallbacks
    def test_register_returns_system_id(self):
        self.installFakeRegionAdvertisingService()
        rack_controller = yield deferToDatabase(factory.make_RackController)
        protocol = self.make_Region()
        protocol.transport = MagicMock()
        response = yield call_responder(
            protocol, RegisterRackController, {
                "system_id": rack_controller.system_id,
                "hostname": rack_controller.hostname,
                "interfaces": {},
            })
        self.assertEquals(
            {"system_id": rack_controller.system_id}, response)

    @wait_for_reactor
    @inlineCallbacks
    def test_register_updates_interfaces(self):
        self.installFakeRegionAdvertisingService()
        rack_controller = yield deferToDatabase(factory.make_RackController)
        protocol = self.make_Region()
        protocol.transport = MagicMock()
        nic_name = factory.make_name("eth0")
        interfaces = {
            nic_name: {
                "type": "physical",
                "mac_address": factory.make_mac_address(),
                "parents": [],
                "links": [],
                "enabled": True,
            }
        }
        response = yield call_responder(
            protocol, RegisterRackController, {
                "system_id": rack_controller.system_id,
                "hostname": rack_controller.hostname,
                "interfaces": interfaces,
            })

        @transactional
        def has_interface(system_id, nic_name):
            rack_controller = RackController.objects.get(system_id=system_id)
            interfaces = rack_controller.interface_set.filter(name=nic_name)
            self.assertThat(interfaces, HasLength(1))
        yield deferToDatabase(has_interface, response["system_id"], nic_name)

    @wait_for_reactor
    @inlineCallbacks
    def test_register_calls_handle_upgrade(self):
        self.installFakeRegionAdvertisingService()
        rack_controller = yield deferToDatabase(factory.make_RackController)
        protocol = self.make_Region()
        protocol.transport = MagicMock()
        ng_uuid = factory.make_UUID()
        mock_handle_upgrade = self.patch(
            regionservice.rackcontrollers, "handle_upgrade")
        yield call_responder(
            protocol, RegisterRackController, {
                "system_id": rack_controller.system_id,
                "hostname": rack_controller.hostname,
                "interfaces": {},
                "nodegroup_uuid": ng_uuid,
            })
        self.assertThat(
            mock_handle_upgrade, MockCalledOnceWith(rack_controller, ng_uuid))

    @wait_for_reactor
    @inlineCallbacks
    def test_register_sets_ident(self):
        self.installFakeRegionAdvertisingService()
        rack_controller = yield deferToDatabase(factory.make_RackController)
        protocol = self.make_Region()
        protocol.transport = MagicMock()
        yield call_responder(
            protocol, RegisterRackController, {
                "system_id": rack_controller.system_id,
                "hostname": rack_controller.hostname,
                "interfaces": {},
            })
        self.assertEquals(rack_controller.system_id, protocol.ident)

    @wait_for_reactor
    @inlineCallbacks
    def test_register_calls_addConnectionFor(self):
        self.installFakeRegionAdvertisingService()
        rack_controller = yield deferToDatabase(factory.make_RackController)
        protocol = self.make_Region()
        protocol.transport = MagicMock()
        mock_addConnectionFor = self.patch(
            protocol.factory.service, "_addConnectionFor")
        yield call_responder(
            protocol, RegisterRackController, {
                "system_id": rack_controller.system_id,
                "hostname": rack_controller.hostname,
                "interfaces": {},
            })
        self.assertThat(
            mock_addConnectionFor,
            MockCalledOnceWith(rack_controller.system_id, protocol))

    @wait_for_reactor
    @inlineCallbacks
    def test_register_sets_hosts(self):
        self.installFakeRegionAdvertisingService()
        rack_controller = yield deferToDatabase(factory.make_RackController)
        protocol = self.make_Region()
        protocol.transport = MagicMock()
        protocol.transport.getHost.return_value = sentinel.host
        yield call_responder(
            protocol, RegisterRackController, {
                "system_id": rack_controller.system_id,
                "hostname": rack_controller.hostname,
                "interfaces": {},
            })
        self.assertEquals(sentinel.host, protocol.host)

    @wait_for_reactor
    @inlineCallbacks
    def test_register_sets_hostIsRemote_calls_registerConnection(self):
        self.installFakeRegionAdvertisingService()
        rack_controller = yield deferToDatabase(factory.make_RackController)
        protocol = self.make_Region()
        protocol.transport = MagicMock()
        host = IPv4Address(
            type='TCP', host=factory.make_ipv4_address(),
            port=random.randint(1, 400))
        protocol.transport.getHost.return_value = host
        mock_deferToDatabase = self.patch(regionservice, "deferToDatabase")
        mock_deferToDatabase.side_effect = [
            succeed((rack_controller, False)),
            succeed(None),
        ]
        yield call_responder(
            protocol, RegisterRackController, {
                "system_id": rack_controller.system_id,
                "hostname": rack_controller.hostname,
                "interfaces": {},
            })
        self.assertTrue(sentinel.host, protocol.hostIsRemote)
        self.assertThat(
            mock_deferToDatabase,
            MockAnyCall(registerConnection, ANY, ANY, host))

    @wait_for_reactor
    @inlineCallbacks
    def test_register_creates_new_rack(self):
        self.installFakeRegionAdvertisingService()
        protocol = self.make_Region()
        protocol.transport = MagicMock()
        hostname = factory.make_hostname()
        yield call_responder(
            protocol, RegisterRackController, {
                "system_id": None,
                "hostname": hostname,
                "interfaces": {},
            })
        yield deferToDatabase(
            RackController.objects.get, hostname=hostname)

    @skip("XXX: GavinPanella 2016-03-09 bug=1555236: Fails spuriously.")
    @wait_for_reactor
    @inlineCallbacks
    def test_register_calls_refresh_when_needed(self):
        protocol = self.make_Region()
        protocol.transport = MagicMock()
        mock_gethost = self.patch(protocol.transport, 'getHost')
        mock_gethost.return_value = IPv4Address(
            type='TCP', host=factory.make_ipv4_address(),
            port=random.randint(1, 65535))
        mock_refresh = self.patch(RackController, 'refresh')
        self.patch(regionservice, 'registerConnection')
        hostname = factory.make_hostname()
        yield call_responder(
            protocol, RegisterRackController, {
                "system_id": None,
                "hostname": hostname,
                "interfaces": {},
            })
        self.assertThat(mock_refresh, MockCalledOnce())

    @wait_for_reactor
    @inlineCallbacks
    def test_register_raises_CannotRegisterRackController_when_it_cant(self):
        self.installFakeRegionAdvertisingService()
        patched_create = self.patch(RackController.objects, 'create')
        patched_create.side_effect = IntegrityError()
        hostname = factory.make_name("hostname")
        error = yield assert_fails_with(
            call_responder(self.make_Region(), RegisterRackController,
                           {"system_id": None,
                            "hostname": hostname,
                            "interfaces": {}}),
            CannotRegisterRackController)
        self.assertEquals((
            "Failed to register rack controller 'None' into the database. "
            "Connection has been dropped.",), error.args)


class TestRegionService(MAASTestCase):

    def test_init_sets_appropriate_instance_attributes(self):
        service = RegionService()
        self.assertThat(service, IsInstance(Service))
        self.assertThat(service.connections, IsInstance(defaultdict))
        self.assertThat(service.connections.default_factory, Is(set))
        self.assertThat(
            service.endpoints, AllMatch(
                AllMatch(Provides(IStreamServerEndpoint))))
        self.assertThat(service.factory, IsInstance(Factory))
        self.assertThat(service.factory.protocol, Equals(RegionServer))
        self.assertThat(service.events.connected, IsInstance(events.Event))
        self.assertThat(service.events.disconnected, IsInstance(events.Event))

    @wait_for_reactor
    def test_starting_and_stopping_the_service(self):
        service = RegionService()
        self.assertThat(service.starting, Is(None))
        service.startService()
        self.assertThat(service.starting, IsInstance(Deferred))

        def check_started(_):
            # Ports are saved as private instance vars.
            self.assertThat(service.ports, HasLength(1))
            [port] = service.ports
            self.assertThat(port, IsInstance(tcp.Port))
            self.assertThat(port.factory, IsInstance(Factory))
            self.assertThat(port.factory.protocol, Equals(RegionServer))
            return service.stopService()

        service.starting.addCallback(check_started)

        def check_stopped(ignore, service=service):
            self.assertThat(service.ports, Equals([]))

        service.starting.addCallback(check_stopped)

        return service.starting

    @wait_for_reactor
    def test_startService_returns_Deferred(self):
        service = RegionService()

        # Don't configure any endpoints.
        self.patch(service, "endpoints", [])

        d = service.startService()
        self.assertThat(d, IsInstance(Deferred))
        # It's actually the `starting` Deferred.
        self.assertIs(service.starting, d)

        def started(_):
            return service.stopService()

        return d.addCallback(started)

    @wait_for_reactor
    def test_start_up_can_be_cancelled(self):
        service = RegionService()

        # Return an inert Deferred from the listen() call.
        endpoints = self.patch(service, "endpoints", [[Mock()]])
        endpoints[0][0].listen.return_value = Deferred()

        service.startService()
        self.assertThat(service.starting, IsInstance(Deferred))

        service.starting.cancel()

        def check(port):
            self.assertThat(port, Is(None))
            self.assertThat(service.ports, HasLength(0))
            return service.stopService()

        return service.starting.addCallback(check)

    @wait_for_reactor
    @inlineCallbacks
    def test_start_up_errors_are_logged(self):
        service = RegionService()

        # Ensure that endpoint.listen fails with a obvious error.
        exception = ValueError("This is not the messiah.")
        endpoints = self.patch(service, "endpoints", [[Mock()]])
        endpoints[0][0].listen.return_value = fail(exception)

        logged_failures = []
        self.patch(log, "msg", (
            lambda failure, **kw: logged_failures.append(failure)))

        logged_failures_expected = [
            AfterPreprocessing(
                (lambda failure: failure.value),
                Is(exception)),
        ]

        yield service.startService()
        self.assertThat(
            logged_failures, MatchesListwise(logged_failures_expected))

    @wait_for_reactor
    @inlineCallbacks
    def test_start_up_binds_first_of_endpoint_options(self):
        service = RegionService()

        endpoint_1 = Mock()
        endpoint_1.listen.return_value = succeed(sentinel.port1)
        endpoint_2 = Mock()
        endpoint_2.listen.return_value = succeed(sentinel.port2)
        service.endpoints = [[endpoint_1, endpoint_2]]

        yield service.startService()

        self.assertThat(service.ports, Equals([sentinel.port1]))

    @wait_for_reactor
    @inlineCallbacks
    def test_start_up_binds_first_of_real_endpoint_options(self):
        service = RegionService()

        # endpoint_1.listen(...) will bind to a random high-numbered port.
        endpoint_1 = TCP4ServerEndpoint(reactor, 0)
        # endpoint_2.listen(...), if attempted, will crash because only root
        # (or a user with explicit capabilities) can do stuff like that. It's
        # a reasonable assumption that the user running these tests is not
        # root, but we'll check the port number later too to be sure.
        endpoint_2 = TCP4ServerEndpoint(reactor, 1)

        service.endpoints = [[endpoint_1, endpoint_2]]

        yield service.startService()
        self.addCleanup(wait_for_reactor(service.stopService))

        # A single port has been bound.
        self.assertThat(service.ports, MatchesAll(
            HasLength(1), AllMatch(IsInstance(tcp.Port))))

        # The port is not listening on port 1; i.e. a belt-n-braces check that
        # endpoint_2 was not used.
        [port] = service.ports
        self.assertThat(port.getHost().port, Not(Equals(1)))

    @wait_for_reactor
    @inlineCallbacks
    def test_start_up_binds_first_successful_of_endpoint_options(self):
        service = RegionService()

        endpoint_broken = Mock()
        endpoint_broken.listen.return_value = fail(factory.make_exception())
        endpoint_okay = Mock()
        endpoint_okay.listen.return_value = succeed(sentinel.port)
        service.endpoints = [[endpoint_broken, endpoint_okay]]

        yield service.startService()

        self.assertThat(service.ports, Equals([sentinel.port]))

    @skip("XXX test fails far too often; bug #1582944")
    @wait_for_reactor
    @inlineCallbacks
    def test_start_up_logs_failure_if_all_endpoint_options_fail(self):
        service = RegionService()

        error_1 = factory.make_exception_type()
        error_2 = factory.make_exception_type()

        endpoint_1 = Mock()
        endpoint_1.listen.return_value = fail(error_1())
        endpoint_2 = Mock()
        endpoint_2.listen.return_value = fail(error_2())
        service.endpoints = [[endpoint_1, endpoint_2]]

        with TwistedLoggerFixture() as logger:
            yield service.startService()

        self.assertDocTestMatches(
            """\
            RegionServer endpoint failed to listen.
            Traceback (most recent call last):
            ...
            %s:
            """ % fullyQualifiedName(error_2),
            logger.output)

    @wait_for_reactor
    def test_stopping_cancels_startup(self):
        service = RegionService()

        # Return an inert Deferred from the listen() call.
        endpoints = self.patch(service, "endpoints", [[Mock()]])
        endpoints[0][0].listen.return_value = Deferred()

        service.startService()
        service.stopService()

        def check(_):
            # The CancelledError is suppressed.
            self.assertThat(service.ports, HasLength(0))

        return service.starting.addCallback(check)

    @wait_for_reactor
    @inlineCallbacks
    def test_stopping_closes_connections_cleanly(self):
        service = RegionService()
        service.starting = Deferred()
        service.factory.protocol = HandshakingRegionServer
        connections = {
            service.factory.buildProtocol(None),
            service.factory.buildProtocol(None),
        }
        for conn in connections:
            # Pretend it's already connected.
            service.connections[conn.ident].add(conn)
        transports = {
            self.patch(conn, "transport")
            for conn in connections
        }
        yield service.stopService()
        self.assertThat(
            transports, AllMatch(
                AfterPreprocessing(
                    attrgetter("loseConnection"),
                    MockCalledOnceWith())))

    @wait_for_reactor
    @inlineCallbacks
    def test_stopping_logs_errors_when_closing_connections(self):
        service = RegionService()
        service.starting = Deferred()
        service.factory.protocol = HandshakingRegionServer
        connections = {
            service.factory.buildProtocol(None),
            service.factory.buildProtocol(None),
        }
        for conn in connections:
            transport = self.patch(conn, "transport")
            transport.loseConnection.side_effect = OSError("broken")
            # Pretend it's already connected.
            service.connections[conn.ident].add(conn)
        logger = self.useFixture(TwistedLoggerFixture())
        # stopService() completes without returning an error.
        yield service.stopService()
        # Connection-specific errors are logged.
        self.assertDocTestMatches(
            """\
            Unhandled Error
            Traceback (most recent call last):
            ...
            builtins.OSError: broken
            ---
            Unhandled Error
            Traceback (most recent call last):
            ...
            builtins.OSError: broken
            """,
            logger.dump())

    @wait_for_reactor
    def test_stopping_when_start_up_failed(self):
        service = RegionService()

        # Ensure that endpoint.listen fails with a obvious error.
        exception = ValueError("This is a very naughty boy.")
        endpoints = self.patch(service, "endpoints", [[Mock()]])
        endpoints[0][0].listen.return_value = fail(exception)
        # Suppress logged messages.
        self.patch(globalLogPublisher, "_observers", [])

        service.startService()
        # The test is that stopService() succeeds.
        return service.stopService()

    @wait_for_reactor
    def test_getClientFor_errors_when_no_connections(self):
        service = RegionService()
        service.connections.clear()
        return assert_fails_with(
            service.getClientFor(factory.make_UUID(), timeout=0),
            exceptions.NoConnectionsAvailable)

    @wait_for_reactor
    def test_getClientFor_errors_when_no_connections_for_cluster(self):
        service = RegionService()
        uuid = factory.make_UUID()
        service.connections[uuid].clear()
        return assert_fails_with(
            service.getClientFor(uuid, timeout=0),
            exceptions.NoConnectionsAvailable)

    @wait_for_reactor
    def test_getClientFor_returns_random_connection(self):
        c1 = DummyConnection()
        c2 = DummyConnection()
        chosen = DummyConnection()

        service = RegionService()
        uuid = factory.make_UUID()
        conns_for_uuid = service.connections[uuid]
        conns_for_uuid.update({c1, c2})

        def check_choice(choices):
            self.assertItemsEqual(choices, conns_for_uuid)
            return chosen
        self.patch(random, "choice", check_choice)

        def check(client):
            self.assertThat(client, Equals(common.Client(chosen)))

        return service.getClientFor(uuid).addCallback(check)

    @wait_for_reactor
    def test_getAllClients_empty(self):
        service = RegionService()
        service.connections.clear()
        self.assertThat(service.getAllClients(), Equals([]))

    @wait_for_reactor
    def test_getAllClients(self):
        service = RegionService()
        uuid1 = factory.make_UUID()
        c1 = DummyConnection()
        c2 = DummyConnection()
        service.connections[uuid1].update({c1, c2})
        uuid2 = factory.make_UUID()
        c3 = DummyConnection()
        c4 = DummyConnection()
        service.connections[uuid2].update({c3, c4})
        clients = service.getAllClients()
        self.assertItemsEqual(clients, {
            common.Client(c1), common.Client(c2),
            common.Client(c3), common.Client(c4),
        })

    def test_addConnectionFor_adds_connection(self):
        service = RegionService()
        uuid = factory.make_UUID()
        c1 = DummyConnection()
        c2 = DummyConnection()

        service._addConnectionFor(uuid, c1)
        service._addConnectionFor(uuid, c2)

        self.assertEqual({uuid: {c1, c2}}, service.connections)

    def test_addConnectionFor_notifies_waiters(self):
        service = RegionService()
        uuid = factory.make_UUID()
        c1 = DummyConnection()
        c2 = DummyConnection()

        waiter1 = Mock()
        waiter2 = Mock()
        service.waiters[uuid].add(waiter1)
        service.waiters[uuid].add(waiter2)

        service._addConnectionFor(uuid, c1)
        service._addConnectionFor(uuid, c2)

        self.assertEqual({uuid: {c1, c2}}, service.connections)
        # Both mock waiters are called twice. A real waiter would only be
        # called once because it immediately unregisters itself once called.
        self.assertThat(
            waiter1.callback,
            MockCallsMatch(call(c1), call(c2)))
        self.assertThat(
            waiter2.callback,
            MockCallsMatch(call(c1), call(c2)))

    def test_addConnectionFor_fires_connected_event(self):
        service = RegionService()
        uuid = factory.make_UUID()
        c1 = DummyConnection()

        mock_fire = self.patch(service.events.connected, "fire")
        service._addConnectionFor(uuid, c1)

        self.assertThat(mock_fire, MockCalledOnceWith(uuid))

    def test_removeConnectionFor_removes_connection(self):
        service = RegionService()
        uuid = factory.make_UUID()
        c1 = DummyConnection()
        c2 = DummyConnection()

        service._addConnectionFor(uuid, c1)
        service._addConnectionFor(uuid, c2)
        service._removeConnectionFor(uuid, c1)

        self.assertEqual({uuid: {c2}}, service.connections)

    def test_removeConnectionFor_is_okay_if_connection_is_not_there(self):
        service = RegionService()
        uuid = factory.make_UUID()

        service._removeConnectionFor(uuid, DummyConnection())

        self.assertEqual({uuid: set()}, service.connections)

    def test_removeConnectionFor_fires_disconnected_event(self):
        service = RegionService()
        uuid = factory.make_UUID()
        c1 = DummyConnection()

        mock_fire = self.patch(service.events.disconnected, "fire")
        service._removeConnectionFor(uuid, c1)

        self.assertThat(mock_fire, MockCalledOnceWith(uuid))

    @wait_for_reactor
    def test_getConnectionFor_returns_existing_connection(self):
        service = RegionService()
        uuid = factory.make_UUID()
        conn = DummyConnection()

        service._addConnectionFor(uuid, conn)

        d = service._getConnectionFor(uuid, 1)
        # No waiter is added because a connection is available.
        self.assertEqual({uuid: set()}, service.waiters)

        def check(conn_returned):
            self.assertEquals(conn, conn_returned)

        return d.addCallback(check)

    @wait_for_reactor
    def test_getConnectionFor_waits_for_connection(self):
        service = RegionService()
        uuid = factory.make_UUID()
        conn = DummyConnection()

        # Add the connection later (we're in the reactor thread right
        # now so this won't happen until after we return).
        reactor.callLater(0, service._addConnectionFor, uuid, conn)

        d = service._getConnectionFor(uuid, 1)
        # A waiter is added for the connection we're interested in.
        self.assertEqual({uuid: {d}}, service.waiters)

        def check(conn_returned):
            self.assertEqual(conn, conn_returned)
            # The waiter has been unregistered.
            self.assertEqual({uuid: set()}, service.waiters)

        return d.addCallback(check)

    @wait_for_reactor
    def test_getConnectionFor_with_concurrent_waiters(self):
        service = RegionService()
        uuid = factory.make_UUID()
        conn = DummyConnection()

        # Add the connection later (we're in the reactor thread right
        # now so this won't happen until after we return).
        reactor.callLater(0, service._addConnectionFor, uuid, conn)

        d1 = service._getConnectionFor(uuid, 1)
        d2 = service._getConnectionFor(uuid, 1)
        # A waiter is added for each call to _getConnectionFor().
        self.assertEqual({uuid: {d1, d2}}, service.waiters)

        d = DeferredList((d1, d2))

        def check(results):
            self.assertEqual(
                [(True, conn), (True, conn)], results)
            # The waiters have both been unregistered.
            self.assertEqual({uuid: set()}, service.waiters)

        return d.addCallback(check)

    @wait_for_reactor
    def test_getConnectionFor_cancels_waiter_when_it_times_out(self):
        service = RegionService()
        uuid = factory.make_UUID()

        d = service._getConnectionFor(uuid, 1)
        # A waiter is added for the connection we're interested in.
        self.assertEqual({uuid: {d}}, service.waiters)
        d = assert_fails_with(d, CancelledError)

        def check(_):
            # The waiter has been unregistered.
            self.assertEqual({uuid: set()}, service.waiters)

        return d.addCallback(check)


class TestRegionAdvertisingService(MAASTransactionServerTestCase):

    run_tests_with = MAASCrochetRunTest

    def setUp(self):
        super(TestRegionAdvertisingService, self).setUp()
        self.maas_id = None

        def set_maas_id(maas_id):
            self.maas_id = maas_id

        self.set_maas_id = self.patch(regionservice, "set_maas_id")
        self.set_maas_id.side_effect = set_maas_id

        def get_maas_id():
            return self.maas_id

        self.get_maas_id = self.patch(regionservice, "get_maas_id")
        self.get_maas_id.side_effect = get_maas_id

    def test_init(self):
        ras = RegionAdvertisingService()
        self.assertThat(
            ras.advertiser, MatchesAll(
                IsInstance(LoopingCall),
                MatchesStructure.byEquality(f=ras._tryUpdate, a=(), kw={}),
                first_only=True,
            ))
        self.assertThat(
            ras.advertising, MatchesAll(
                IsInstance(DeferredValue),
                MatchesStructure.byEquality(isSet=False),
                first_only=True,
            ))

    @wait_for_reactor
    @inlineCallbacks
    def test_try_update_logs_all_errors(self):
        ras = RegionAdvertisingService()
        # Prevent periodic calls to `update`.
        ras._startAdvertising = always_succeed_with(None)
        ras._stopAdvertising = always_succeed_with(None)
        # Start the service and make sure it stops later.
        yield ras.startService()
        try:
            # Ensure that calls to `advertising.update` will crash.
            advertising = yield ras.advertising.get(0.0)
            advertising_update = self.patch(advertising, "update")
            advertising_update.side_effect = factory.make_exception()

            with TwistedLoggerFixture() as logger:
                yield ras._tryUpdate()
            self.assertDocTestMatches(
                """
                Failed to update regiond's process and endpoints;
                  %s record's may be out of date
                Traceback (most recent call last):
                ...
                maastesting.factory.TestException#...
                """ % eventloop.loop.name,
                logger.output)
        finally:
            yield ras.stopService()

    @wait_for_reactor
    @inlineCallbacks
    def test_starting_and_stopping_the_service(self):
        service = RegionAdvertisingService()

        self.assertThat(service.starting, Is(None))
        starting = service.startService()
        try:
            # The service is already marked as running.
            self.assertTrue(service.running)
            # Wait for start-up to fully complete.
            self.assertThat(service.starting, IsInstance(Deferred))
            self.assertThat(service.starting, Is(starting))
            yield service.starting
            # A RegionController has been created.
            region_ids = yield deferToDatabase(lambda: {
                region.system_id for region in RegionController.objects.all()})
            self.assertThat(region_ids, HasLength(1))
            # The maas_id file has been created too.
            region_id = region_ids.pop()
            self.assertThat(self.set_maas_id, MockCalledOnceWith(region_id))
            # Finally, the advertising value has been set.
            advertising = yield service.advertising.get(0.0)
            self.assertThat(
                advertising, MatchesAll(
                    IsInstance(RegionAdvertising),
                    MatchesStructure.byEquality(region_id=region_id),
                    first_only=True,
                ))
        finally:
            self.assertThat(service.stopping, Is(None))
            stopping = service.stopService()
            # The service is already marked as NOT running.
            self.assertFalse(service.running)
            # Wait for shut-down to fully complete.
            self.assertThat(service.stopping, IsInstance(Deferred))
            self.assertThat(service.stopping, Is(stopping))
            yield service.stopping

    @wait_for_reactor
    @inlineCallbacks
    def test_start_up_errors_are_logged(self):
        service = RegionAdvertisingService()
        # Prevent real pauses.
        self.patch_autospec(regionservice, "pause").return_value = None
        # Make service._getAdvertisingInfo fail the first time it's called.
        exceptions = [ValueError("You don't vote for kings!")]
        original = service._getAdvertisingInfo

        def _getAdvertisingInfo():
            if len(exceptions) == 0:
                return original()
            else:
                raise exceptions.pop(0)

        gao = self.patch(service, "_getAdvertisingInfo")
        gao.side_effect = _getAdvertisingInfo
        # Capture all Twisted logs.
        logger = self.useFixture(TwistedLoggerFixture())

        yield service.startService()
        try:
            self.assertDocTestMatches(
                """\
                Promotion of ... failed; will try again in 5 seconds.
                Traceback (most recent call last):...
                builtins.ValueError: You don't vote for kings!
                """,
                logger.dump())
        finally:
            yield service.stopService()

    def test_stopping_waits_for_startup(self):
        service = RegionAdvertisingService()
        synchronise = threading.Condition()

        # Prevent the advertising loop from starting.
        service._startAdvertising = lambda: None
        service._stopAdvertising = lambda: None

        # Prevent the service's _getAdvertisingInfo method - which is deferred
        # to a thread - from completing while we hold the lock.
        def _getAdvertisingInfo(original=service._getAdvertisingInfo):
            with synchronise:
                synchronise.notify()
                synchronise.wait(2.0)
            return original()
        service._getAdvertisingInfo = _getAdvertisingInfo

        with synchronise:
            # Start the service, but stop it again before promote is able to
            # complete.
            service.startService()
            synchronise.wait(2.0)
            service.stopService()
            synchronise.notify()

        callInReactorWithTimeout(5.0, lambda: service.starting)
        callInReactorWithTimeout(5.0, lambda: service.stopping)
        self.assertFalse(service.running)

    def test_stopping_when_start_up_failed(self):
        service = RegionAdvertisingService()

        # Ensure that service.promote fails with a obvious error.
        exception = ValueError("First, shalt thou take out the holy pin.")
        self.patch(service, "promote").side_effect = exception

        # Start the service, but don't wait.
        service.startService()
        # The test is that stopService() succeeds.
        service.stopService().wait(10)

    def patch_port(self, port):
        getServiceNamed = self.patch(eventloop.services, "getServiceNamed")
        getPort = getServiceNamed.return_value.getPort
        getPort.return_value = port

    def patch_addresses(self, addresses):
        get_all_interface_addresses = self.patch(
            regionservice, "get_all_interface_addresses")
        get_all_interface_addresses.return_value = addresses

    @wait_for_reactor
    @inlineCallbacks
    def test_stopping_demotes_region(self):
        service = RegionAdvertisingService()
        service._getAddresses = always_succeed_with({("192.168.0.1", 9876)})

        yield service.startService()
        yield service.stopService()

        dump = yield deferToDatabase(RegionAdvertising.dump)
        self.assertItemsEqual([], dump)

    def test__getAddresses_excluding_loopback(self):
        service = RegionAdvertisingService()

        example_port = factory.pick_port()
        self.patch_port(example_port)

        example_ipv4_addrs = set()
        for _ in range(5):
            ip = factory.make_ipv4_address()
            if not netaddr.IPAddress(ip).is_loopback():
                example_ipv4_addrs.add(ip)
        example_ipv6_addrs = set()
        for _ in range(5):
            ip = factory.make_ipv6_address()
            if not netaddr.IPAddress(ip).is_loopback():
                example_ipv6_addrs.add(ip)
        example_link_local_addrs = {
            factory.pick_ip_in_network(netaddr.ip.IPV4_LINK_LOCAL),
            factory.pick_ip_in_network(netaddr.ip.IPV6_LINK_LOCAL),
        }
        example_loopback_addrs = {
            factory.pick_ip_in_network(netaddr.ip.IPV4_LOOPBACK),
            str(netaddr.ip.IPV6_LOOPBACK),
        }
        self.patch_addresses(
            example_ipv4_addrs | example_ipv6_addrs |
            example_link_local_addrs | example_loopback_addrs)

        # IPv6 addresses, link-local addresses and loopback are excluded, and
        # thus not advertised.
        self.assertItemsEqual(
            [(addr, example_port) for addr in example_ipv4_addrs],
            service._getAddresses().wait(2.0))

        self.assertThat(
            eventloop.services.getServiceNamed,
            MockCalledOnceWith("rpc"))
        self.assertThat(
            regionservice.get_all_interface_addresses,
            MockCalledOnceWith())

    def test__getAddresses_including_loopback(self):
        service = RegionAdvertisingService()

        example_port = factory.pick_port()
        self.patch_port(example_port)

        example_link_local_addrs = {
            factory.pick_ip_in_network(netaddr.ip.IPV4_LINK_LOCAL),
            factory.pick_ip_in_network(netaddr.ip.IPV6_LINK_LOCAL),
        }
        ipv4_loopback = factory.pick_ip_in_network(netaddr.ip.IPV4_LOOPBACK)
        example_loopback_addrs = {
            ipv4_loopback,
            str(netaddr.ip.IPV6_LOOPBACK),
        }
        self.patch_addresses(
            example_link_local_addrs | example_loopback_addrs)

        # Only IPv4 loopback is exposed.
        self.assertItemsEqual(
            [(ipv4_loopback, example_port)],
            service._getAddresses().wait(2.0))

        self.assertThat(
            eventloop.services.getServiceNamed,
            MockCalledOnceWith("rpc"))
        self.assertThat(
            regionservice.get_all_interface_addresses,
            MockCalledOnceWith())

    def test__getAddresses_when_rpc_down(self):
        service = RegionAdvertisingService()

        # getPort() returns None when the RPC service is not running or
        # not able to bind a port.
        self.patch_port(None)

        get_all_interface_addresses = self.patch(
            regionservice, "get_all_interface_addresses")
        get_all_interface_addresses.return_value = [
            factory.make_ipv4_address(),
            factory.make_ipv4_address(),
        ]

        # If the RPC service is down, _getAddresses() returns nothing.
        self.assertItemsEqual([], service._getAddresses().wait(2.0))


class TestRegionAdvertising(MAASServerTestCase):

    hostname = gethostname()

    def promote(self, region_id=None, hostname=hostname, mac_addresses=None):
        """Convenient wrapper around `RegionAdvertising.promote`."""
        return RegionAdvertising.promote(
            factory.make_name("region-id") if region_id is None else region_id,
            hostname, [] if mac_addresses is None else mac_addresses)

    def make_addresses(self):
        """Return a set of a couple of ``(addr, port)`` tuples."""
        return {
            (factory.make_ipv4_address(), factory.pick_port()),
            (factory.make_ipv4_address(), factory.pick_port()),
        }

    def get_endpoints(self, region_id):
        """Return a set of ``(addr, port)`` tuples for the given region."""
        region = RegionController.objects.get(system_id=region_id)
        return {
            (endpoint.address, endpoint.port)
            for process in region.processes.all()
            for endpoint in process.endpoints.all()
        }

    def test_promote_new_region(self):
        # Before promotion there are no RegionControllers.
        self.assertEquals(
            0, RegionController.objects.count(),
            "No RegionControllers should exist.")

        advertising = self.promote()

        # Now a RegionController exists for the given hostname.
        region = RegionController.objects.get(hostname=gethostname())
        self.assertThat(advertising.region_id, Equals(region.system_id))

    def test_promote_converts_from_node(self):
        node = factory.make_Node(interface=True)
        interfaces = [
            factory.make_Interface(node=node),
            factory.make_Interface(node=node),
        ]
        mac_addresses = [
            str(interface.mac_address)
            for interface in interfaces
        ]

        self.promote(node.system_id, self.hostname, mac_addresses)

        # Node should have been converted to a RegionController.
        node = reload_object(node)
        self.assertEquals(NODE_TYPE.REGION_CONTROLLER, node.node_type)
        # The hostname has also been set.
        self.assertEquals(self.hostname, node.hostname)

    def test_promote_converts_from_rack(self):
        node = factory.make_Node(
            interface=True, node_type=NODE_TYPE.RACK_CONTROLLER)
        interface = node.get_boot_interface()
        mac_address = str(interface.mac_address)

        self.promote(node.system_id, self.hostname, [mac_address])

        # Node should have been converted to a RegionRackController.
        node = reload_object(node)
        self.assertEquals(NODE_TYPE.REGION_AND_RACK_CONTROLLER, node.node_type)
        # The hostname has also been set.
        self.assertEquals(self.hostname, node.hostname)

    def test_promote_sets_region_hostname(self):
        node = factory.make_Node(node_type=NODE_TYPE.REGION_CONTROLLER)

        self.promote(node.system_id, self.hostname)

        # The hostname has been set.
        self.assertEquals(self.hostname, reload_object(node).hostname)

    def test_promote_holds_startup_lock(self):
        # Creating tables in PostgreSQL is a transactional operation like any
        # other. If the isolation level is not sufficient it is susceptible to
        # races. Using a higher isolation level may lead to serialisation
        # failures, for example. However, PostgreSQL provides advisory locking
        # functions, and that's what RegionAdvertising.promote takes advantage
        # of to prevent concurrent creation of the region controllers.

        # A record of the lock's status, populated when a custom
        # patched-in _do_create() is called.
        locked = []

        # Capture the state of `locks.eventloop` while `promote` is running.
        original_fix_node_for_region = regionservice.fix_node_for_region

        def fix_node_for_region(*args, **kwargs):
            locked.append(locks.eventloop.is_locked())
            return original_fix_node_for_region(*args, **kwargs)

        fnfr = self.patch(regionservice, "fix_node_for_region")
        fnfr.side_effect = fix_node_for_region

        # `fix_node_for_region` is only called for preexisting nodes.
        node = factory.make_Node(node_type=NODE_TYPE.REGION_CONTROLLER)

        # The lock is not held before and after `promote` is called.
        self.assertFalse(locks.eventloop.is_locked())
        self.promote(node.system_id)
        self.assertFalse(locks.eventloop.is_locked())

        # The lock was held when `fix_node_for_region` was called.
        self.assertEqual([True], locked)

    def test_update_updates_region_hostname(self):
        advertising = self.promote()

        region = RegionController.objects.get(system_id=advertising.region_id)
        region.hostname = factory.make_name("host")
        region.save()

        advertising.update(self.make_addresses())

        # RegionController should have hostname updated.
        region = reload_object(region)
        self.assertEquals(self.hostname, region.hostname)

    def test_update_creates_process_when_removed(self):
        advertising = self.promote()

        region = RegionController.objects.get(system_id=advertising.region_id)
        [process] = region.processes.all()
        process_id = process.id
        process.delete()

        # Will re-create the process with the same ID.
        advertising.update(self.make_addresses())

        process.id = process_id
        process = reload_object(process)
        self.assertEquals(process.pid, os.getpid())

    def test_update_removes_old_processes(self):
        advertising = self.promote()

        old_time = now() - timedelta(seconds=90)
        region = RegionController.objects.get(system_id=advertising.region_id)
        other_region = factory.make_Node(node_type=NODE_TYPE.REGION_CONTROLLER)
        old_region_process = RegionControllerProcess.objects.create(
            region=region, pid=randint(1, 1000), created=old_time,
            updated=old_time)
        old_other_region_process = RegionControllerProcess.objects.create(
            region=other_region, pid=randint(1000, 2000), created=old_time,
            updated=old_time)

        advertising.update(self.make_addresses())

        self.assertIsNone(reload_object(old_region_process))
        self.assertIsNone(reload_object(old_other_region_process))

    def test_update_updates_updated_time_on_region_and_process(self):
        current_time = now()
        self.patch(timestampedmodel, "now").return_value = current_time

        advertising = self.promote()

        old_time = current_time - timedelta(seconds=90)
        region = RegionController.objects.get(system_id=advertising.region_id)
        region.created = old_time
        region.updated = old_time
        region.save()
        region_process = RegionControllerProcess.objects.get(
            id=advertising.process_id)
        region_process.created = region_process.updated = old_time
        region_process.save()

        advertising.update(self.make_addresses())

        region = reload_object(region)
        region_process = reload_object(region_process)
        self.assertEquals(current_time, region.updated)
        self.assertEquals(current_time, region_process.updated)

    def test_update_creates_endpoints_on_process(self):
        addresses = self.make_addresses()

        advertising = self.promote()
        advertising.update(addresses)

        saved_endpoints = self.get_endpoints(advertising.region_id)
        self.assertEqual(addresses, saved_endpoints)

    def test_update_does_not_insert_endpoints_when_nothings_listening(self):
        advertising = self.promote()
        advertising.update(set())  # No addresses.

        saved_endpoints = self.get_endpoints(advertising.region_id)
        self.assertEqual(set(), saved_endpoints)

    def test_update_deletes_old_endpoints(self):
        addresses_common = self.make_addresses()
        addresses_one = self.make_addresses().union(addresses_common)
        addresses_two = self.make_addresses().union(addresses_common)

        advertising = self.promote()

        advertising.update(addresses_one)
        self.assertEqual(
            addresses_one, self.get_endpoints(advertising.region_id))

        advertising.update(addresses_two)
        self.assertEqual(
            addresses_two, self.get_endpoints(advertising.region_id))

    def test_update_sets_regiond_degraded_with_less_than_4_processes(self):
        advertising = self.promote()
        advertising.update(self.make_addresses())

        region = RegionController.objects.get(system_id=advertising.region_id)
        [process] = region.processes.all()
        regiond_service = ServiceModel.objects.get(node=region, name="regiond")
        self.assertThat(regiond_service, MatchesStructure.byEquality(
            status=SERVICE_STATUS.DEGRADED,
            status_info="1 process running but 4 were expected."))

    def test_update_sets_regiond_running_with_4_processes(self):
        advertising = self.promote()

        region = RegionController.objects.get(system_id=advertising.region_id)
        [process] = region.processes.all()

        # Make 3 more processes.
        for _ in range(3):
            factory.make_RegionControllerProcess(region=region)

        advertising.update(self.make_addresses())

        regiond_service = ServiceModel.objects.get(node=region, name="regiond")
        self.assertThat(regiond_service, MatchesStructure.byEquality(
            status=SERVICE_STATUS.RUNNING, status_info=""))

    def test_update_calls_mark_dead_on_regions_without_processes(self):
        advertising = self.promote()

        other_region = factory.make_RegionController()
        mock_mark_dead = self.patch(ServiceModel.objects, "mark_dead")

        advertising.update(self.make_addresses())

        self.assertThat(
            mock_mark_dead,
            MockCalledOnceWith(other_region, dead_region=True))

    def test_demote(self):
        region_id = factory.make_name("region-id")
        hostname = gethostname()
        addresses = {
            (factory.make_ipv4_address(), factory.pick_port()),
            (factory.make_ipv4_address(), factory.pick_port()),
        }
        advertising = RegionAdvertising.promote(region_id, hostname, [])
        advertising.update(addresses)
        advertising.demote()
        self.assertItemsEqual([], advertising.dump())

    def test_dump(self):
        region_id = factory.make_name("region-id")
        hostname = gethostname()
        addresses = {
            (factory.make_ipv4_address(), factory.pick_port()),
            (factory.make_ipv4_address(), factory.pick_port()),
        }
        advertising = RegionAdvertising.promote(region_id, hostname, [])
        advertising.update(addresses)

        expected = [
            ("%s:pid=%d" % (hostname, os.getpid()), addr, port)
            for (addr, port) in addresses
        ]
        self.assertItemsEqual(expected, advertising.dump())