# Copyright (c) 2026 Jacek Fedorynski
# SPDX-License-Identifier: MIT
#
# Adapted from USBSignalInEndpoint in LUNA
# (luna/gateware/usb/usb2/endpoints/status.py), which is:
#
# Copyright (c) 2020 Great Scott Gadgets <info@greatscottgadgets.com>
# SPDX-License-Identifier: BSD-3-Clause

"""Interrupt IN endpoints that send the value a signal has at the moment the
PHY takes the byte."""

from amaranth import Array, Elaboratable, Module, Signal

from luna.gateware.usb.usb2.endpoint import EndpointInterface


class LiveByteInEndpoint(Elaboratable):
    """Answers every IN token with a one-byte DATA packet holding `value`.

    LUNA's USBSignalInEndpoint latches its signal when the response is
    started, before the PID goes out. Here the value isn't latched at all:
    `value` is wired through to the transmitter, so what's sent is whatever it
    is in the cycle the PHY accepts the payload byte (TxReady, which is ULPI
    NXT). The CRC generator reads the same wires in the same cycle.

    Parameters
    ----------
    endpoint_number: int
        The endpoint number (not address) this endpoint should respond to.
    value: Signal(8), optional
        The byte to send, if not the endpoint's own `value` signal. Has to be
        synchronous to the `usb` domain.

    Attributes
    ----------
    value: Signal(8), input
        The byte to send.
    interface: EndpointInterface
        Communications link to our USB device.
    """

    def __init__(self, *, endpoint_number, value=None):
        self._endpoint_number = endpoint_number

        self.value     = Signal(8) if value is None else value
        self.interface = EndpointInterface()

    def elaborate(self, platform):
        m = Module()

        interface = self.interface
        tx        = interface.tx
        tokenizer = interface.tokenizer

        m.d.comb += tx.payload.eq(self.value)

        packet_requested = \
            (tokenizer.endpoint == self._endpoint_number) & tokenizer.is_in & tokenizer.ready_for_response

        with m.FSM(domain="usb"):

            with m.State('IDLE'):
                with m.If(packet_requested):
                    m.next = 'TRANSMIT'

            with m.State('TRANSMIT'):
                m.d.comb += [
                    tx.valid .eq(1),
                    tx.first .eq(1),
                    tx.last  .eq(1),
                ]
                with m.If(tx.ready):
                    m.next = 'WAIT_FOR_ACK'

            # A packet that isn't ACKed isn't sent again as it was: the next
            # IN token gets the current value, with the same data PID.
            with m.State('WAIT_FOR_ACK'):
                with m.If(interface.handshakes_in.ack):
                    m.d.usb += interface.tx_pid_toggle[0].eq(~interface.tx_pid_toggle[0])
                    m.next = 'IDLE'
                with m.If(tokenizer.new_token):
                    m.next = 'IDLE'

        return m


class LiveReportInEndpoint(Elaboratable):
    """Answers every IN token with a DATA packet of `report`'s bytes, each one
    with the value it has in the cycle the PHY accepts that byte.

    The same as LiveByteInEndpoint, a byte at a time: nothing is latched when
    the packet starts, so a byte that's sent late in the packet is read late.

    Parameters
    ----------
    endpoint_number: int
        The endpoint number (not address) this endpoint should respond to.
    report: list of 8-bit values
        The bytes to send, in order. Signals have to be synchronous to the
        `usb` domain.

    Attributes
    ----------
    interface: EndpointInterface
        Communications link to our USB device.
    """

    def __init__(self, *, endpoint_number, report):
        self._endpoint_number = endpoint_number
        self._report = list(report)

        self.interface = EndpointInterface()

    def elaborate(self, platform):
        m = Module()

        interface = self.interface
        tx        = interface.tx
        tokenizer = interface.tokenizer

        last_index = len(self._report) - 1
        index = Signal(range(len(self._report)))

        m.d.comb += tx.payload.eq(Array(self._report)[index])

        packet_requested = \
            (tokenizer.endpoint == self._endpoint_number) & tokenizer.is_in & tokenizer.ready_for_response

        with m.FSM(domain="usb"):

            with m.State('IDLE'):
                m.d.usb += index.eq(0)
                with m.If(packet_requested):
                    m.next = 'TRANSMIT'

            with m.State('TRANSMIT'):
                m.d.comb += [
                    tx.valid .eq(1),
                    tx.first .eq(index == 0),
                    tx.last  .eq(index == last_index),
                ]
                with m.If(tx.ready):
                    m.d.usb += index.eq(index + 1)
                    with m.If(index == last_index):
                        m.next = 'WAIT_FOR_ACK'

            # A packet that isn't ACKed isn't sent again as it was: the next
            # IN token gets the current values, with the same data PID.
            with m.State('WAIT_FOR_ACK'):
                with m.If(interface.handshakes_in.ack):
                    m.d.usb += interface.tx_pid_toggle[0].eq(~interface.tx_pid_toggle[0])
                    m.next = 'IDLE'
                with m.If(tokenizer.new_token):
                    m.next = 'IDLE'

        return m
