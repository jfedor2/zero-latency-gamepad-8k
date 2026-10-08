# Copyright (c) 2026 Jacek Fedorynski
# SPDX-License-Identifier: MIT

"""Simulates the endpoint with LUNA's data packet generator, playing the
tokenizer and the PHY.

Run from the repository's directory: python -m unittest tests.test_endpoint
"""

import unittest

from amaranth import Elaboratable, Module
from amaranth.sim import Simulator

from luna.gateware.usb.usb2.packet import USBDataPacketGenerator

from gateware.endpoint import LiveByteInEndpoint

ENDPOINT = 1


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        for bit in range(8):
            if (crc ^ (byte >> bit)) & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc ^ 0xFFFF


class Harness(Elaboratable):
    def __init__(self):
        self.endpoint = LiveByteInEndpoint(endpoint_number=ENDPOINT)
        self.generator = USBDataPacketGenerator(standalone=True)

    def elaborate(self, platform):
        m = Module()
        m.submodules.endpoint = self.endpoint
        m.submodules.generator = self.generator
        m.d.comb += [
            self.generator.stream.stream_eq(self.endpoint.interface.tx),
            self.generator.data_pid.eq(self.endpoint.interface.tx_pid_toggle),
        ]
        return m


class EndpointTest(unittest.TestCase):

    def run_sim(self, testbench):
        h = Harness()
        sim = Simulator(h)
        sim.add_clock(1 / 60e6, domain="usb")

        async def bench(ctx):
            await testbench(ctx, h)

        sim.add_testbench(bench)
        sim.run()

    async def poll(self, ctx, h, values, wait=1, endpoint=ENDPOINT, ack=True):
        """An IN token, then the PHY takes a byte every `wait` cycles.
        values: what the endpoint's input is in each cycle from the start of
        the response; the last one stays. Returns the packet's bytes and the
        cycle each was taken in."""
        interface = h.endpoint.interface
        tx = h.generator.tx

        ctx.set(interface.tokenizer.endpoint, endpoint)
        ctx.set(interface.tokenizer.is_in, 1)
        ctx.set(interface.tokenizer.new_token, 1)
        await ctx.tick("usb")
        ctx.set(interface.tokenizer.new_token, 0)
        await ctx.tick("usb").repeat(3)
        ctx.set(interface.tokenizer.ready_for_response, 1)

        packet = []
        cycles = []
        idle = 0
        for cycle in range(200):
            ctx.set(h.endpoint.value, values[min(cycle, len(values) - 1)])
            ready = cycle % wait == wait - 1
            ctx.set(tx.ready, ready)
            if ctx.get(tx.valid):
                idle = 0
                if ready:
                    packet.append(ctx.get(tx.data))
                    cycles.append(cycle)
            else:
                idle += 1
                if idle > 2 * wait + 4:
                    break
            await ctx.tick("usb")
            ctx.set(interface.tokenizer.ready_for_response, 0)
        ctx.set(tx.ready, 0)

        if packet and ack:
            ctx.set(interface.handshakes_in.ack, 1)
            await ctx.tick("usb")
            ctx.set(interface.handshakes_in.ack, 0)
        await ctx.tick("usb")
        return packet, cycles

    def check(self, packet, pid, value):
        self.assertEqual(len(packet), 4)
        self.assertEqual(packet[0], pid)
        self.assertEqual(packet[1], value)
        self.assertEqual(packet[2] | (packet[3] << 8), crc16(packet[1:2]))

    def test_value_at_the_moment_the_byte_is_taken(self):
        for wait in (1, 2, 5, 40):
            with self.subTest(wait=wait):
                async def testbench(ctx, h):
                    # Find out which cycle the payload byte is taken in.
                    _, cycles = await self.poll(ctx, h, [0x00], wait=wait)
                    payload_cycle = cycles[1]

                    # A different value in every cycle: that cycle's is sent.
                    values = [(3 * n + 1) & 0xFF for n in range(payload_cycle + 20)]
                    packet, cycles = await self.poll(ctx, h, values, wait=wait)
                    self.assertEqual(cycles[1], payload_cycle)
                    self.check(packet, 0x4B, values[payload_cycle])

                    # Changing in that very cycle is in time, a cycle later isn't.
                    values = [0x00] * payload_cycle + [0x5A, 0xFF]
                    packet, _ = await self.poll(ctx, h, values, wait=wait)
                    self.check(packet, 0xC3, 0x5A)

                self.run_sim(testbench)

    def test_data_toggle(self):
        async def testbench(ctx, h):
            packet, _ = await self.poll(ctx, h, [0x01])
            self.check(packet, 0xC3, 0x01)
            packet, _ = await self.poll(ctx, h, [0x02], ack=False)
            self.check(packet, 0x4B, 0x02)
            # Not ACKed: the same PID again, with the current value.
            packet, _ = await self.poll(ctx, h, [0x03])
            self.check(packet, 0x4B, 0x03)
            packet, _ = await self.poll(ctx, h, [0x04])
            self.check(packet, 0xC3, 0x04)

        self.run_sim(testbench)

    def test_other_endpoint(self):
        async def testbench(ctx, h):
            packet, _ = await self.poll(ctx, h, [0x01], endpoint=2)
            self.assertEqual(packet, [])

        self.run_sim(testbench)
