# Copyright (c) 2026 Jacek Fedorynski
# SPDX-License-Identifier: MIT

"""Simulates the whole USB device, with LUNA's test host on its UTMI bus.

Run from the repository's directory: python -m unittest tests.test_device
"""

from amaranth import Signal

from usb_protocol.types import USBPacketID

from luna.usb2 import USBDevice
from luna.gateware.test.usb2 import USBDeviceTest
from luna.gateware.test.utils import usb_domain_test_case

from gateware.top import (
    add_endpoints, HID_REPORT_DESCRIPTOR, HID_DESCRIPTOR, HID_ENDPOINT_NUMBER,
    DESCRIPTOR_TYPE_HID_REPORT, HID_REQUEST_GET_REPORT, HID_REQUEST_SET_IDLE)

CLASS_INTERFACE_IN = 0xA1
CLASS_INTERFACE_OUT = 0x21
STANDARD_INTERFACE_IN = 0x81
GET_DESCRIPTOR = 6
HID_REQUEST_GET_IDLE = 0x02


class DeviceTest(USBDeviceTest):
    FRAGMENT_UNDER_TEST = USBDevice
    FRAGMENT_ARGUMENTS = {'handle_clocking': False}

    def provision_dut(self, dut):
        self.report = Signal(8)
        add_endpoints(dut, self.report)

    def initialize_signals(self):
        yield self.utmi.tx_ready.eq(1)
        yield self.dut.connect.eq(1)

    @usb_domain_test_case
    def test_enumeration(self):
        yield from self.set_address(5)

        handshake, data = yield from self.get_descriptor(2, length=64)
        self.assertEqual(handshake, USBPacketID.ACK)
        data = bytes(data)
        self.assertEqual(len(data), 9 + 9 + 9 + 7)
        self.assertEqual(data[2], len(data))
        # HID, no subclass, no protocol.
        self.assertEqual(data[9 + 5:9 + 8], bytes([3, 0, 0]))
        self.assertEqual(data[18:27], HID_DESCRIPTOR)
        # Interrupt IN, one byte, bInterval 1.
        self.assertEqual(data[27:], bytes([7, 5, 0x80 | HID_ENDPOINT_NUMBER, 3, 1, 0, 1]))

        yield from self.set_configuration(1)

        # The way hosts ask for it: recipient interface.
        handshake, data = yield from self.control_request_in(
            STANDARD_INTERFACE_IN, GET_DESCRIPTOR, value=DESCRIPTOR_TYPE_HID_REPORT << 8, index=0,
            length=len(HID_REPORT_DESCRIPTOR))
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(bytes(data), HID_REPORT_DESCRIPTOR)

    @usb_domain_test_case
    def test_other_speed_descriptors(self):
        handshake, device = yield from self.get_descriptor(1, length=18)
        handshake, configuration = yield from self.get_descriptor(2, length=64)

        handshake, data = yield from self.get_descriptor(6, length=10)
        self.assertEqual(handshake, USBPacketID.ACK)
        # USB 2.0, no class, 64-byte packets on endpoint 0, one configuration.
        self.assertEqual(bytes(data), bytes([10, 6, 0x00, 0x02, 0, 0, 0, 64, 1, 0]))
        self.assertEqual(data[2:8], device[2:8])

        handshake, data = yield from self.get_descriptor(7, length=64)
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(data, [configuration[0], 7] + configuration[2:])

    @usb_domain_test_case
    def test_class_requests(self):
        # Answered with a zero-length packet in the status stage.
        pid = yield from self.control_request_out(CLASS_INTERFACE_OUT, HID_REQUEST_SET_IDLE)
        self.assertEqual(pid, USBPacketID.DATA1)

        yield self.report.eq(0xA5)
        handshake, data = yield from self.control_request_in(
            CLASS_INTERFACE_IN, HID_REQUEST_GET_REPORT, value=0x0100, length=1)
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(data, [0xA5])

        handshake, data = yield from self.control_request_in(CLASS_INTERFACE_IN, HID_REQUEST_GET_IDLE, length=1)
        self.assertEqual(handshake, USBPacketID.STALL)

        # Still works after a stalled request.
        handshake, data = yield from self.get_descriptor(1, length=18)
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(len(data), 18)

    @usb_domain_test_case
    def test_reports(self):
        yield from self.set_configuration(1)

        expected_pid = USBPacketID.DATA0
        for value in (0x00, 0x01, 0x80, 0xFF, 0x5A):
            yield self.report.eq(value)
            pid, data = yield from self.in_transaction(endpoint=HID_ENDPOINT_NUMBER, data_pid=expected_pid)
            self.assertEqual(data, [value])
            expected_pid = USBPacketID.DATA1 if expected_pid == USBPacketID.DATA0 else USBPacketID.DATA0
