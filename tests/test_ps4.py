# Copyright (c) 2026 Jacek Fedorynski
# SPDX-License-Identifier: MIT

"""Simulates the PS4 report (the endpoint with LUNA's data packet generator,
playing the tokenizer and the PHY) and the request that's answered locally
(the whole USB device, with LUNA's test host).

Run from the repository's directory: python -m unittest tests.test_ps4
"""

import unittest

from amaranth import Elaboratable, Module, Signal
from amaranth.sim import Simulator

from luna.gateware.usb.usb2.packet import USBDataPacketGenerator

from usb_protocol.types import USBPacketID

from luna.usb2 import USBDevice
from luna.gateware.test.usb2 import USBDeviceTest
from luna.gateware.test.utils import usb_domain_test_case

from gateware.endpoint import LiveReportInEndpoint
from gateware.ps4 import (
    report_bytes, hat_value, create_descriptors, is_controller_type_request, ControllerTypeRequestHandler,
    HIDClassRequestHandler, REPORT_LENGTH, HAT_NEUTRAL, CONTROLLER_TYPE_REPORT, HID_REQUEST_GET_REPORT)

from .test_endpoint import crc16

ENDPOINT = 1

CLASS_INTERFACE_IN = 0xA1
CLASS_INTERFACE_OUT = 0x21
HID_REQUEST_SET_IDLE = 0x0A

NEUTRAL = [0x01, 0x80, 0x80, 0x80, 0x80, 0x08] + [0] * 58
NEUTRAL[35] = NEUTRAL[39] = 0x80

UP, DOWN, LEFT, RIGHT, SQUARE, CROSS, CIRCLE, TRIANGLE, \
    L1, R1, L2, R2, R3, OPTIONS, PS, TOUCHPAD = (1 << n for n in range(16))


class Harness(Elaboratable):
    def __init__(self):
        self.buttons = Signal(16)
        self.generator = USBDataPacketGenerator(standalone=True)

    def elaborate(self, platform):
        m = Module()
        m.submodules.endpoint = self.endpoint = LiveReportInEndpoint(
            endpoint_number=ENDPOINT, report=report_bytes(m, self.buttons))
        m.submodules.generator = self.generator
        m.d.comb += [
            self.generator.stream.stream_eq(self.endpoint.interface.tx),
            self.generator.data_pid.eq(self.endpoint.interface.tx_pid_toggle),
        ]
        return m


class PS4ReportTest(unittest.TestCase):

    def run_sim(self, testbench):
        h = Harness()
        sim = Simulator(h)
        sim.add_clock(1 / 60e6, domain="usb")

        async def bench(ctx):
            await testbench(ctx, h)

        sim.add_testbench(bench)
        sim.run()

    async def poll(self, ctx, h, buttons, wait=1):
        """An IN token, then the PHY takes a byte every `wait` cycles.
        buttons: called with the number of bytes taken so far, in every
        cycle, for the buttons' state. Returns the packet's payload."""
        interface = h.endpoint.interface
        tx = h.generator.tx

        ctx.set(interface.tokenizer.endpoint, ENDPOINT)
        ctx.set(interface.tokenizer.is_in, 1)
        ctx.set(interface.tokenizer.new_token, 1)
        await ctx.tick("usb")
        ctx.set(interface.tokenizer.new_token, 0)
        await ctx.tick("usb").repeat(3)
        ctx.set(interface.tokenizer.ready_for_response, 1)

        packet = []
        idle = 0
        for cycle in range(2000):
            ctx.set(h.buttons, buttons(len(packet)))
            ready = cycle % wait == wait - 1
            ctx.set(tx.ready, ready)
            if ctx.get(tx.valid):
                idle = 0
                if ready:
                    packet.append(ctx.get(tx.data))
            else:
                idle += 1
                if idle > 2 * wait + 4:
                    break
            await ctx.tick("usb")
            ctx.set(interface.tokenizer.ready_for_response, 0)
        ctx.set(tx.ready, 0)

        ctx.set(interface.handshakes_in.ack, 1)
        await ctx.tick("usb")
        ctx.set(interface.handshakes_in.ack, 0)
        await ctx.tick("usb")

        self.assertEqual(len(packet), 1 + REPORT_LENGTH + 2)
        self.assertIn(packet[0], (0xC3, 0x4B))
        payload = packet[1:-2]
        self.assertEqual(packet[-2] | (packet[-1] << 8), crc16(payload))
        return payload

    def test_layout(self):
        async def testbench(ctx, h):
            self.assertEqual(await self.poll(ctx, h, lambda n: 0), NEUTRAL)

            for buttons, changes in (
                (UP, {5: 0x00}),
                (UP | RIGHT, {5: 0x01}),
                (DOWN | LEFT | CROSS, {5: 0x25}),
                (LEFT | RIGHT, {5: 0x08}),
                (UP | DOWN | LEFT, {5: 0x06}),
                (SQUARE | TRIANGLE, {5: 0x98}),
                (L1 | OPTIONS, {6: 0x21}),
                (R1 | R3, {6: 0x82}),
                (L2, {6: 0x04, 8: 0xFF}),
                (R2, {6: 0x08, 9: 0xFF}),
                (PS, {7: 0x01}),
                (TOUCHPAD, {7: 0x02}),
            ):
                expected = list(NEUTRAL)
                for index, value in changes.items():
                    expected[index] = value
                for wait in (1, 3):
                    with self.subTest(buttons=buttons, wait=wait):
                        self.assertEqual(await self.poll(ctx, h, lambda n: buttons, wait=wait), expected)

        self.run_sim(testbench)

    def test_bytes_are_read_as_they_go_out(self):
        async def testbench(ctx, h):
            # Pressed once four bytes are out: in time for bytes 5 to 7.
            payload = await self.poll(ctx, h, lambda n: (CROSS | L1 | PS) if n >= 5 else 0)
            self.assertEqual(payload[5:8], [0x28, 0x01, 0x01])

            # Once byte 5 is out it's too late for cross, not for L1 and PS.
            payload = await self.poll(ctx, h, lambda n: (CROSS | L1 | PS) if n >= 7 else 0)
            self.assertEqual(payload[5:8], [0x08, 0x01, 0x01])

        self.run_sim(testbench)

    def test_hat(self):
        self.assertEqual(hat_value(0, 0, 0, 0), HAT_NEUTRAL)
        self.assertEqual([hat_value(1, 0, 0, 0), hat_value(1, 0, 0, 1), hat_value(0, 0, 0, 1), hat_value(0, 1, 0, 1),
                          hat_value(0, 1, 0, 0), hat_value(0, 1, 1, 0), hat_value(0, 0, 1, 0), hat_value(1, 0, 1, 0)],
                         list(range(8)))


class ControllerTypeTest(USBDeviceTest):
    """The control endpoint as the gamepad has it, except that hurra-fpga's
    handler has no relay: it then stalls what it would pass on, other than
    SET_IDLE."""

    FRAGMENT_UNDER_TEST = USBDevice
    FRAGMENT_ARGUMENTS = {'handle_clocking': False}

    def provision_dut(self, dut):
        control_endpoint = dut.add_standard_control_endpoint(create_descriptors())
        control_endpoint.add_request_handler(HIDClassRequestHandler(skiplist=[is_controller_type_request]))
        control_endpoint.add_request_handler(ControllerTypeRequestHandler())

    def initialize_signals(self):
        yield self.utmi.tx_ready.eq(1)
        yield self.dut.connect.eq(1)

    @usb_domain_test_case
    def test_other_speed_descriptors(self):
        handshake, configuration = yield from self.get_descriptor(2, length=64)

        handshake, data = yield from self.get_descriptor(6, length=10)
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(bytes(data), bytes([10, 6, 0x00, 0x02, 0, 0, 0, 64, 1, 0]))

        handshake, data = yield from self.get_descriptor(7, length=64)
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(data, [configuration[0], 7] + configuration[2:])

    @usb_domain_test_case
    def test_controller_type_is_answered_here(self):
        handshake, data = yield from self.control_request_in(
            CLASS_INTERFACE_IN, HID_REQUEST_GET_REPORT, value=0x0303, length=48)
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(bytes(data), CONTROLLER_TYPE_REPORT)
        self.assertEqual(len(data), 48)

        # No more than what's asked for, and again from the start.
        handshake, data = yield from self.control_request_in(
            CLASS_INTERFACE_IN, HID_REQUEST_GET_REPORT, value=0x0303, length=16)
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(bytes(data), CONTROLLER_TYPE_REPORT[:16])

        # Asked for more, there isn't more.
        handshake, data = yield from self.control_request_in(
            CLASS_INTERFACE_IN, HID_REQUEST_GET_REPORT, value=0x0303, length=64)
        self.assertEqual(handshake, USBPacketID.ACK)
        self.assertEqual(bytes(data), CONTROLLER_TYPE_REPORT)

    @usb_domain_test_case
    def test_other_requests_are_the_other_handler_s(self):
        # Another feature report: passed on, which without a relay is a stall.
        handshake, data = yield from self.control_request_in(
            CLASS_INTERFACE_IN, HID_REQUEST_GET_REPORT, value=0x03F2, length=16)
        self.assertEqual(handshake, USBPacketID.STALL)

        # Input report 3 isn't feature report 3.
        handshake, data = yield from self.control_request_in(
            CLASS_INTERFACE_IN, HID_REQUEST_GET_REPORT, value=0x0103, length=48)
        self.assertEqual(handshake, USBPacketID.STALL)

        pid = yield from self.control_request_out(CLASS_INTERFACE_OUT, HID_REQUEST_SET_IDLE)
        self.assertEqual(pid, USBPacketID.DATA1)

        handshake, data = yield from self.control_request_in(
            CLASS_INTERFACE_IN, HID_REQUEST_GET_REPORT, value=0x0303, length=48)
        self.assertEqual(bytes(data), CONTROLLER_TYPE_REPORT)
