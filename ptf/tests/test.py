# Copyright 2013-present Barefoot Networks, Inc.
# Copyright 2018-present Open Networking Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import struct
import socket

from p4.v1 import p4runtime_pb2
from ptf import testutils as testutils
from ptf.packet import IPv6
from ptf.testutils import group

from scapy.layers.all import *
from scapy.pton_ntop import inet_pton, inet_ntop
from scapy.utils6 import in6_getnsma, in6_getnsmac

from base_test import P4RuntimeTest, stringify, mac_to_binary, ipv6_to_binary, \
    autocleanup

DEFAULT_PRIORITY = 10

IPV6_MCAST_MAC_1 = "33:33:00:00:00:01"

SWITCH1_MAC = "00:00:00:00:aa:01"
SWITCH2_MAC = "00:00:00:00:aa:02"
SWITCH3_MAC = "00:00:00:00:aa:03"
HOST1_MAC = "00:00:00:00:00:01"
HOST2_MAC = "00:00:00:00:00:02"

SWITCH1_IPV6 = "2001:0:1::1"
SWITCH2_IPV6 = "2001:0:2::1"
SWITCH3_IPV6 = "2001:0:3::1"
HOST1_IPV6 = "2001:0000:85a3::8a2e:370:1111"
HOST2_IPV6 = "2001:0000:85a3::8a2e:370:2222"


def pkt_mac_swap(pkt):
    orig_dst = pkt[Ether].dst
    pkt[Ether].dst = pkt[Ether].src
    pkt[Ether].src = orig_dst
    return pkt


def pkt_route(pkt, mac_dst):
    pkt[Ether].src = pkt[Ether].dst
    pkt[Ether].dst = mac_dst
    return pkt


def pkt_decrement_ttl(pkt):
    if IP in pkt:
        pkt[IP].ttl -= 1
    elif IPv6 in pkt:
        pkt[IPv6].hlim -= 1
    return pkt


class FabricTest(P4RuntimeTest):

    def __init__(self):
        super(FabricTest, self).__init__()
        self.next_mbr_id = 1
        self.next_grp_id = 1

    def setUp(self):
        super(FabricTest, self).setUp()
        self.port1 = self.swports(1)
        self.port2 = self.swports(2)
        self.port3 = self.swports(3)

    def get_next_mbr_id(self):
        mbr_id = self.next_mbr_id
        self.next_mbr_id = self.next_mbr_id + 1
        return mbr_id

    def get_next_grp_id(self):
        grp_id = self.next_grp_id
        self.next_grp_id = self.next_grp_id + 1
        return grp_id

    def add_l2_unicast_entry(self, eth_dstAddr, out_port):
        out_port_ = stringify(out_port, 2)
        self.add_l2_entry(
            eth_dstAddr,
            ["FabricIngress.l2_unicast_fwd", [("port_num", out_port_)]])

    def add_l2_multicast_entry(self, eth_dstAddr, out_ports):
        grp_id = self.get_next_grp_id()
        grp_id_ = stringify(grp_id, 2)
        self.add_mcast_group(grp_id, out_ports)
        self.add_l2_entry(
            eth_dstAddr,
            ["FabricIngress.l2_multicast_fwd", [("gid", grp_id_)]])

    def add_l2_entry(self, eth_dstAddr, action):
        eth_dstAddr_ = mac_to_binary(eth_dstAddr)
        mk = [self.Exact("hdr.ethernet.dst_addr", eth_dstAddr_)]
        self.send_request_add_entry_to_action(
            "FabricIngress.l2_table", mk, *action)

    def add_l2_my_station_entry(self, eth_dstAddr):
        eth_dstAddr_ = mac_to_binary(eth_dstAddr)
        mk = [self.Exact("hdr.ethernet.dst_addr", eth_dstAddr_)]
        self.send_request_add_entry_to_action(
            "FabricIngress.l2_my_station", mk, "NoAction", [])

    def add_l3_entry(self, dstAddr, prefix_len, grp_id):
        dstAddr_ = ipv6_to_binary(dstAddr)
        self.send_request_add_entry_to_group(
            "FabricIngress.l3_table",
            [self.Lpm("hdr.ipv6.dst_addr", dstAddr_, prefix_len)], grp_id)

    # members is list of tuples (action_name, params)
    # params contains a tuple for each param (param_name, param_value)
    def add_l3_group_with_members(self, grp_id, members):
        mbr_ids = []
        for member in members:
            mbr_id = self.get_next_mbr_id()
            mbr_ids.append(mbr_id)
            self.send_request_add_member("FabricIngress.ecmp_selector", mbr_id,
                                         *member)
        self.send_request_add_group("FabricIngress.ecmp_selector", grp_id,
                                    grp_size=len(mbr_ids), mbr_ids=mbr_ids)

    def add_l3_ecmp_entry(self, dstAddr, prefix_len, next_hop_macs):
        members = []
        for mac in next_hop_macs:
            mac_ = mac_to_binary(mac)
            members.append(("FabricIngress.set_l2_next_hop", [("dmac", mac_)]))
        grp_id = self.get_next_grp_id()
        self.add_l3_group_with_members(grp_id, members)
        self.add_l3_entry(dstAddr, prefix_len, grp_id)

    def add_acl_cpu_entry(self, eth_type=None, clone=False):
        eth_type_ = stringify(eth_type, 2)
        eth_type_mask = stringify(0xFFFF, 2)
        action_name = "clone_to_cpu" if clone else "punt_to_cpu"
        self.send_request_add_entry_to_action(
            "FabricIngress.acl",
            [self.Ternary("hdr.ethernet.ether_type", eth_type_, eth_type_mask)],
            "FabricIngress." + action_name, [],
            DEFAULT_PRIORITY)

    def add_mcast_group(self, group_id, ports):
        req = self.get_new_write_request()
        update = req.updates.add()
        update.type = p4runtime_pb2.Update.INSERT
        pre_entry = update.entity.packet_replication_engine_entry
        mg_entry = pre_entry.multicast_group_entry
        mg_entry.multicast_group_id = group_id
        for port in ports:
            replica = mg_entry.replicas.add()
            replica.egress_port = port
            replica.instance = 0
        return req, self.write_request(req)

    def add_ndp_reply_entry(self, target_addr, target_mac):
        target_addr = inet_pton(socket.AF_INET6, target_addr)
        target_mac = mac_to_binary(target_mac)
        mk = [self.Exact("hdr.ndp.target_addr", target_addr)]
        self.send_request_add_entry_to_action(
            "FabricIngress.ndp_reply", mk,
            "FabricIngress.ndp_advertisement", [("router_mac", target_mac)])

    def add_srv6_transit_2segment_entry(self, dst_ip, prefix_len, s1_ip, s2_ip):
                self.send_request_add_entry_to_action(
            "FabricIngress.srv6_transit",
            [self.Lpm("hdr.ipv6.dst_addr", ipv6_to_binary(dst_ip), prefix_len)],
            "FabricIngress.srv6_t_insert_2",
            [("s1", ipv6_to_binary(s1_ip)), ("s2", ipv6_to_binary(s2_ip))]
        )

    def add_srv6_transit_3segment_entry(self, dst_ip, prefix_len, s1_ip, s2_ip, s3_ip):
        self.send_request_add_entry_to_action(
            "FabricIngress.srv6_transit",
            [self.Lpm("hdr.ipv6.dst_addr", ipv6_to_binary(dst_ip), prefix_len)],
            "FabricIngress.srv6_t_insert_3",
            [("s1", ipv6_to_binary(s1_ip)), ("s2", ipv6_to_binary(s2_ip)),
             ("s3", ipv6_to_binary(s3_ip))]
        )

    def add_srv6_my_sid_entry(self, my_sid):
        mask = stringify(0xffffffffffffffffffffffffffffffff, 2)
        self.send_request_add_entry_to_action(
            "FabricIngress.srv6_my_sid",
            [self.Ternary("hdr.ipv6.dst_addr", ipv6_to_binary(my_sid), mask)],
            "FabricIngress.srv6_end",
            [],
            DEFAULT_PRIORITY
        )


class FabricBridgingTest(FabricTest):

    @autocleanup
    def runBridgingTest(self, pkt):
        mac_src = pkt[Ether].src
        mac_dst = pkt[Ether].dst
        # miss on filtering.fwd_classifier => bridging
        self.add_l2_unicast_entry(mac_dst, self.port2)
        self.add_l2_unicast_entry(mac_src, self.port1)
        pkt2 = pkt_mac_swap(pkt.copy())
        testutils.send_packet(self, self.port1, str(pkt))
        testutils.send_packet(self, self.port2, str(pkt2))
        testutils.verify_each_packet_on_each_port(
            self, [pkt, pkt2], [self.port2, self.port1])

    def runTest(self):
        print ""
        for pkt_type in ["tcp", "udp", "icmp", "tcpv6", "udpv6", "icmpv6"]:
            print "Testing %s packet..." % pkt_type
            pkt = getattr(testutils, "simple_%s_packet" % pkt_type)(
                pktlen=120)
            self.runBridgingTest(pkt)

class FabricNdpReplyTest(FabricTest):

    def GenNdpNsPkt(self, src_mac, src_ip, target_ip):
        nsma = in6_getnsma(inet_pton(socket.AF_INET6, target_ip))
        d = inet_ntop(socket.AF_INET6, nsma)
        dm = in6_getnsmac(nsma)
        p = Ether(dst=dm) / IPv6(dst=d, src=src_ip, hlim=255)
        p /= ICMPv6ND_NS(tgt=target_ip)
        p /= ICMPv6NDOptSrcLLAddr(lladdr=src_mac)
        return p

    def GenNdpNaPkt(self, src_mac, dst_mac, src_ip, dst_ip):
        p = Ether(src=src_mac, dst=dst_mac)
        p /= IPv6(dst=dst_ip, src=src_ip, hlim=255)
        p /= ICMPv6ND_NA(tgt=src_ip)
        p /= ICMPv6NDOptDstLLAddr(lladdr=src_mac)
        return p

    @autocleanup
    def runTest(self):
        pkt = self.GenNdpNsPkt(HOST1_MAC, HOST1_IPV6, SWITCH1_IPV6)
        pkt_expect = self.GenNdpNaPkt(SWITCH1_MAC, IPV6_MCAST_MAC_1, SWITCH1_IPV6, HOST1_IPV6)
        self.add_ndp_reply_entry(SWITCH1_IPV6, SWITCH1_MAC)
        testutils.send_packet(self, self.port1, str(pkt))
        testutils.verify_packet(self, pkt_expect, self.port1)


class FabricIPv6UnicastTest(FabricTest):

    @autocleanup
    def doRunTest(self, pkt, next_hop_mac, prefix_len=128):
        if IPv6 not in pkt or Ether not in pkt:
            self.fail("Cannot do IPv6 test with packet that is not IPv6")
        self.add_l2_my_station_entry(pkt[Ether].dst)
        self.add_l3_ecmp_entry(pkt[IPv6].dst, prefix_len, [next_hop_mac])
        self.add_l2_unicast_entry(next_hop_mac, self.port2)
        exp_pkt = pkt.copy()
        pkt_route(exp_pkt, next_hop_mac)
        pkt_decrement_ttl(exp_pkt)
        testutils.send_packet(self, self.port1, str(pkt))
        testutils.verify_packet(self, exp_pkt, self.port2)

    def runTest(self):
        print ""
        for pkt_type in ["tcpv6", "udpv6", "icmpv6"]:
            print "Testing %s packet..." % pkt_type
            pkt = getattr(testutils, "simple_%s_packet" % pkt_type)(
                eth_src=HOST1_MAC, eth_dst=SWITCH1_MAC,
                ipv6_src=HOST1_IPV6, ipv6_dst=HOST2_IPV6
            )
            self.doRunTest(pkt, HOST2_MAC)

@group("srv6")
class FabricSrv6InsertTest(FabricTest):
    '''
    l2_my_station -> srv6_transit -> l3_table -> l2_table
    '''

    @autocleanup
    def doRunTest(self, pkt, sid_list):
        sid_len = len(sid_list)
        if IPv6 not in pkt or Ether not in pkt:
            self.fail("Cannot do IPv6 test with packet that is not IPv6")
        self.add_l2_my_station_entry(SWITCH1_MAC)
        getattr(self, "add_srv6_transit_%dsegment_entry" % sid_len)(pkt[IPv6].dst, 128, *sid_list)
        self.add_l3_ecmp_entry(sid_list[0], 128, [SWITCH2_MAC])
        self.add_l2_unicast_entry(SWITCH2_MAC, self.port2)
        testutils.send_packet(self, self.port1, str(pkt))

        exp_pkt = Ether(src=SWITCH1_MAC, dst=SWITCH2_MAC)
        exp_pkt /= IPv6(dst=sid_list[0], src=pkt[IPv6].src, hlim=63)
        exp_pkt /= IPv6ExtHdrSegmentRouting(nh=pkt[IPv6].nh,
                                            addresses=sid_list[::-1],
                                            len=sid_len * 2, segleft=sid_len - 1, lastentry=sid_len - 1)
        exp_pkt /= pkt[IPv6].payload

        if ICMPv6EchoRequest in exp_pkt:
            # FIXME: the P4 pipeline should calculate correct ICMPv6 checksum
            exp_pkt[ICMPv6EchoRequest].cksum = pkt[ICMPv6EchoRequest].cksum
        testutils.verify_packet(self, exp_pkt, self.port2)

    def runTest(self):
        sid_lists = (
            [SWITCH2_IPV6, SWITCH3_IPV6, HOST2_IPV6],
            [SWITCH3_IPV6, HOST2_IPV6],
        )
        for sid_list in sid_lists:
            for pkt_type in ["tcpv6", "udpv6", "icmpv6"]:
                print "Testing %s packet with %d segments ..." % (pkt_type, len(sid_list))
                pkt = getattr(testutils, "simple_%s_packet" % pkt_type)(
                    eth_src=HOST1_MAC, eth_dst=SWITCH1_MAC,
                    ipv6_src=HOST1_IPV6, ipv6_dst=HOST2_IPV6
                )
                self.doRunTest(pkt, sid_list)

@group("srv6")
class FabricSrv6TransitTest(FabricTest):
    '''
    l2_my_station -> l3_table -> l2_table
    No changes to SRH header
    '''

    @autocleanup
    def doRunTest(self, pkt):
        if IPv6 not in pkt or Ether not in pkt:
            self.fail("Cannot do IPv6 test with packet that is not IPv6")

        self.add_l2_my_station_entry(SWITCH2_MAC)
        self.add_srv6_my_sid_entry(SWITCH2_IPV6)
        self.add_l3_ecmp_entry(SWITCH3_IPV6, 128, [SWITCH3_MAC])
        self.add_l2_unicast_entry(SWITCH3_MAC, self.port2)

        testutils.send_packet(self, self.port1, str(pkt))

        exp_pkt = Ether(src=SWITCH2_MAC, dst=SWITCH3_MAC)
        exp_pkt /= IPv6(dst=SWITCH3_IPV6, src=pkt[IPv6].src, hlim=63)
        exp_pkt /= IPv6ExtHdrSegmentRouting(nh=pkt[IPv6ExtHdrSegmentRouting].nh,
                                            addresses=[HOST2_IPV6, SWITCH3_IPV6],
                                            len=2 * 2, segleft=1, lastentry=1)
        exp_pkt /= pkt[IPv6ExtHdrSegmentRouting].payload

        testutils.verify_packet(self, exp_pkt, self.port2)

    def runTest(self):
        pkt = Ether(src=SWITCH1_MAC, dst=SWITCH2_MAC)
        pkt /= IPv6(dst=SWITCH3_IPV6, src=HOST1_IPV6, hlim=64)
        pkt /= IPv6ExtHdrSegmentRouting(nh=6,
                                        addresses=[HOST2_IPV6, SWITCH3_IPV6],
                                        len=2 * 2, segleft=1, lastentry=1)
        pkt /= TCP()

        self.doRunTest(pkt)


@group("srv6")
class FabricSrv6EndTest(FabricTest):
    '''
    l2_my_station -> my_sid -> l3_table -> l2_table
    Decrement SRH SL (after transform SL > 0)
    '''

    @autocleanup
    def doRunTest(self, pkt):
        if IPv6 not in pkt or Ether not in pkt:
            self.fail("Cannot do IPv6 test with packet that is not IPv6")

        self.add_l2_my_station_entry(SWITCH2_MAC)
        self.add_srv6_my_sid_entry(SWITCH2_IPV6)
        self.add_l3_ecmp_entry(SWITCH3_IPV6, 128, [SWITCH3_MAC])
        self.add_l2_unicast_entry(SWITCH3_MAC, self.port2)

        testutils.send_packet(self, self.port1, str(pkt))

        exp_pkt = Ether(src=SWITCH2_MAC, dst=SWITCH3_MAC)
        exp_pkt /= IPv6(dst=SWITCH3_IPV6, src=pkt[IPv6].src, hlim=63)
        exp_pkt /= IPv6ExtHdrSegmentRouting(nh=pkt[IPv6ExtHdrSegmentRouting].nh,
                                            addresses=[HOST2_IPV6, SWITCH3_IPV6, SWITCH2_IPV6],
                                            len=3 * 2, segleft=1, lastentry=2)
        exp_pkt /= pkt[IPv6ExtHdrSegmentRouting].payload

        testutils.verify_packet(self, exp_pkt, self.port2)

    def runTest(self):
        pkt = Ether(src=SWITCH1_MAC, dst=SWITCH2_MAC)
        pkt /= IPv6(dst=SWITCH2_IPV6, src=HOST1_IPV6, hlim=64)
        pkt /= IPv6ExtHdrSegmentRouting(nh=6,
                                        addresses=[HOST2_IPV6, SWITCH3_IPV6, SWITCH2_IPV6],
                                        len=3 * 2, segleft=2, lastentry=2)
        pkt /= TCP()

        self.doRunTest(pkt)

@group("srv6")
class FabricSrv6EndPspTest(FabricTest):
    '''
    l2_my_station -> my_sid -> l3_table -> l2_table
    Decrement SRH SL (after transform SL == 0)
    '''

    @autocleanup
    def doRunTest(self, pkt):
        if IPv6 not in pkt or Ether not in pkt:
            self.fail("Cannot do IPv6 test with packet that is not IPv6")

        self.add_l2_my_station_entry(SWITCH3_MAC)
        self.add_srv6_my_sid_entry(SWITCH3_IPV6)
        self.add_l3_ecmp_entry(HOST2_IPV6, 128, [HOST2_MAC])
        self.add_l2_unicast_entry(HOST2_MAC, self.port2)

        testutils.send_packet(self, self.port1, str(pkt))

        exp_pkt = Ether(src=SWITCH3_MAC, dst=HOST2_MAC)
        exp_pkt /= IPv6(dst=HOST2_IPV6, src=pkt[IPv6].src, hlim=63, nh=pkt[IPv6ExtHdrSegmentRouting].nh)
        exp_pkt /= pkt[IPv6ExtHdrSegmentRouting].payload

        testutils.verify_packet(self, exp_pkt, self.port2)

    def runTest(self):
        pkt = Ether(src=SWITCH2_MAC, dst=SWITCH3_MAC)
        pkt /= IPv6(dst=SWITCH3_IPV6, src=HOST1_IPV6, hlim=64)
        pkt /= IPv6ExtHdrSegmentRouting(nh=6,
                                        addresses=[HOST2_IPV6, SWITCH3_IPV6, SWITCH2_IPV6],
                                        len=3 * 2, segleft=1, lastentry=2)
        pkt /= TCP()

        self.doRunTest(pkt)


@group("packetio")
class FabricPacketOutTest(FabricTest):

    def runPacketOutTest(self, pkt):
        for port in [self.port1, self.port2]:
            port_hex = stringify(port, 2)
            packet_out = p4runtime_pb2.PacketOut()
            packet_out.payload = str(pkt)
            egress_physical_port = packet_out.metadata.add()
            egress_physical_port.metadata_id = 1
            egress_physical_port.value = port_hex

            self.send_packet_out(packet_out)
            testutils.verify_packet(self, pkt, port)
        testutils.verify_no_other_packets(self)

    @autocleanup
    def runTest(self):
        print ""
        for pkt_type in ["tcp", "udp", "icmp", "arp", "tcpv6", "udpv6",
                         "icmpv6"]:
            print "Testing %s packet..." % pkt_type
            pkt = getattr(testutils, "simple_%s_packet" % pkt_type)()
            self.runPacketOutTest(pkt)


@group("packetio")
class FabricPacketInTest(FabricTest):

    @autocleanup
    def runPacketInTest(self, pkt, eth_type=None):
        if eth_type is None:
            eth_type = pkt[Ether].type
        self.add_acl_cpu_entry(eth_type=eth_type)
        for port in [self.port1, self.port2, self.port3]:
            testutils.send_packet(self, port, str(pkt))
            self.verify_packet_in(pkt, port)
        testutils.verify_no_other_packets(self)

    def runTest(self):
        print ""
        for pkt_type in ["tcp", "udp", "icmp", "arp", "tcpv6", "udpv6",
                         "icmpv6"]:
            print "Testing %s packet..." % pkt_type
            pkt = getattr(testutils, "simple_%s_packet" % pkt_type)()
            self.runPacketInTest(pkt)


class FabricArpBroadcastWithCloneTest(FabricTest):

    @autocleanup
    def runTest(self):
        ports = [self.port1, self.port2, self.port3]
        pkt = testutils.simple_arp_packet()
        # FIXME: use clone session APIs when supported on PI
        # For now we add the CPU port to the mc group.
        self.add_l2_multicast_entry(pkt[Ether].dst, ports + [self.cpu_port])
        self.add_acl_cpu_entry(eth_type=pkt[Ether].type, clone=True)

        for inport in ports:
            testutils.send_packet(self, inport, str(pkt))
            # Pkt should be received on CPU and on all ports
            # except the ingress one.
            self.verify_packet_in(exp_pkt=pkt, exp_in_port=inport)
            verify_ports = set(ports)
            verify_ports.discard(inport)
            for port in verify_ports:
                testutils.verify_packet(self, pkt, port)
        testutils.verify_no_other_packets(self)
