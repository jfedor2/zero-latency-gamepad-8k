#!/usr/bin/env python3
# Copyright (c) 2026 Jacek Fedorynski
# SPDX-License-Identifier: MIT

"""Zero latency gamepad for the Cynthion, as a PS4 arcade stick.

The gamepad on the AUX port presents itself the way HID Remapper
(https://github.com/jfedor2/hid-remapper) does in its PS4 mode. A licensed
PS4 controller or authentication dongle goes on TARGET-A: the HID class
requests the console makes (the authentication is GET_REPORT and SET_REPORT of
feature reports 0xF0 to 0xF3) are passed on to it as they are, and its
answers passed back. The console is NAKed while that happens.

The one request that's answered here is GET_REPORT of feature report 0x03,
which says what kind of controller this is: an arcade stick's answer, whatever
the device on TARGET-A is.

The USB host on TARGET-A, the relay and the request handler are hurra-fpga's
(https://github.com/VoltCyclone/hurra-fpga, MIT licensed), from the
deps/hurra-fpga submodule. $HURRA_SRC points the build at another copy's src
directory instead.

Build (LUNA_PLATFORM has to be set, see README.md):
    python -m gateware.ps4 --output gamepad-ps5.bit

The buttons are on PMOD A and PMOD B. They have pull-ups; a pin connected to
GND is a pressed button.

    PMOD A pin   1     2     3     4      7       8      9       10
                 up    down  left  right  square  cross  circle  triangle
    PMOD B pin   1     2     3     4      7       8        9     10
                 L1    R1    L2    R2     R3      options  PS    touchpad

LEDs: 0 a device is connected to TARGET-A, 1 it's enumerated (the gamepad
only appears on AUX then), 2 a request is being passed on to it, 3 one that
got no proper answer (stays on; the device refusing a request with a STALL
is an answer), 4 the host gave up on the device, 5 any button.
"""

import os
import sys
from pathlib import Path

from amaranth import Signal, Elaboratable, Module, Cat, Array, Mux, C
from amaranth.build import Resource, Pins, Attrs
from amaranth.lib.cdc import FFSynchronizer

from usb_protocol.emitters import DeviceDescriptorCollection
from usb_protocol.types import USBRequestType, USBTransferType

from luna import top_level_cli
from luna.usb2 import USBDevice
from luna.gateware.interface.ulpi import UTMITranslator
from luna.gateware.usb.request.control import ControlRequestHandler
from luna.gateware.usb.stream import USBInStreamInterface
from luna.gateware.usb.usb2.request import StallOnlyRequestHandler
from luna.gateware.stream.generator import ConstantStreamGenerator

from .endpoint import LiveReportInEndpoint
from .top import hid_descriptor, DESCRIPTOR_TYPE_HID_REPORT, add_other_speed_descriptors

sys.path.insert(0, os.environ.get("HURRA_SRC", str(Path(__file__).resolve().parents[1] / "deps" / "hurra-fpga" / "src")))
try:
    from hurra_cynthion.host import BoundedMouseHost  # noqa: E402
    from hurra_cynthion.device_control import HIDClassRequestHandler  # noqa: E402
    from hurra_cynthion.types import HostError, TransactionStatus  # noqa: E402
except ImportError as e:
    raise ImportError(f"{e}; the hurra-fpga submodule isn't checked out? "
                      "Run: git submodule update --init") from e

USB_VENDOR_ID = 0xCAFE
USB_PRODUCT_ID = 0xBAE1

HID_ENDPOINT_NUMBER = 1
REPORT_LENGTH = 64

# How long the console is NAKed waiting for the device on TARGET-A to answer
# a request, before it's stalled: 250 ms. (hurra-fpga's own limit is 20 ms.)
RELAY_TIMEOUT_CYCLES = 15_000_000

HID_REPORT_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop Ctrls)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x09, 0x30,        #   Usage (X)
    0x09, 0x31,        #   Usage (Y)
    0x09, 0x32,        #   Usage (Z)
    0x09, 0x35,        #   Usage (Rz)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x04,        #   Report Count (4)
    0x81, 0x02,        #   Input (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0x09, 0x39,        #   Usage (Hat switch)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x07,        #   Logical Maximum (7)
    0x35, 0x00,        #   Physical Minimum (0)
    0x46, 0x3B, 0x01,  #   Physical Maximum (315)
    0x65, 0x14,        #   Unit (System: English Rotation, Length: Centimeter)
    0x75, 0x04,        #   Report Size (4)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x42,        #   Input (Data,Var,Abs,No Wrap,Linear,Preferred State,Null State)
    0x65, 0x00,        #   Unit (None)
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (0x01)
    0x29, 0x0E,        #   Usage Maximum (0x0E)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x0E,        #   Report Count (14)
    0x81, 0x02,        #   Input (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0x06, 0x00, 0xFF,  #   Usage Page (Vendor Defined 0xFF00)
    0x09, 0x20,        #   Usage (0x20)
    0x75, 0x06,        #   Report Size (6)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x02,        #   Input (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0x05, 0x01,        #   Usage Page (Generic Desktop Ctrls)
    0x09, 0x33,        #   Usage (Rx)
    0x09, 0x34,        #   Usage (Ry)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0x06, 0x00, 0xFF,  #   Usage Page (Vendor Defined 0xFF00)
    0x09, 0x21,        #   Usage (0x21)
    0x95, 0x36,        #   Report Count (54)
    0x81, 0x02,        #   Input (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0x85, 0x05,        #   Report ID (5)
    0x09, 0x22,        #   Usage (0x22)
    0x95, 0x1F,        #   Report Count (31)
    0x91, 0x02,        #   Output (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position,Non-volatile)
    0x85, 0x03,        #   Report ID (3)
    0x0A, 0x21, 0x27,  #   Usage (0x2721)
    0x95, 0x2F,        #   Report Count (47)
    0xB1, 0x02,        #   Feature (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position,Non-volatile)
    0x06, 0x80, 0xFF,  #   Usage Page (Vendor Defined 0xFF80)
    0x85, 0xE0,        #   Report ID (-32)
    0x09, 0x57,        #   Usage (0x57)
    0x95, 0x02,        #   Report Count (2)
    0xB1, 0x02,        #   Feature (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position,Non-volatile)
    0xC0,              # End Collection
    0x06, 0xF0, 0xFF,  # Usage Page (Vendor Defined 0xFFF0)
    0x09, 0x40,        # Usage (0x40)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0xF0,        #   Report ID (-16)
    0x09, 0x47,        #   Usage (0x47)
    0x95, 0x3F,        #   Report Count (63)
    0xB1, 0x02,        #   Feature (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position,Non-volatile)
    0x85, 0xF1,        #   Report ID (-15)
    0x09, 0x48,        #   Usage (0x48)
    0x95, 0x3F,        #   Report Count (63)
    0xB1, 0x02,        #   Feature (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position,Non-volatile)
    0x85, 0xF2,        #   Report ID (-14)
    0x09, 0x49,        #   Usage (0x49)
    0x95, 0x0F,        #   Report Count (15)
    0xB1, 0x02,        #   Feature (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position,Non-volatile)
    0x85, 0xF3,        #   Report ID (-13)
    0x0A, 0x01, 0x47,  #   Usage (0x4701)
    0x95, 0x07,        #   Report Count (7)
    0xB1, 0x02,        #   Feature (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position,Non-volatile)
    0xC0,              # End Collection
])

HID_REQUEST_GET_REPORT = 0x01
HID_REPORT_TYPE_FEATURE = 3

# Feature report 0x03, what kind of controller this is: a Razer Raion's, an
# arcade stick. (As HID Remapper answers it.)
CONTROLLER_TYPE_REPORT_ID = 0x03
CONTROLLER_TYPE_REQUEST_VALUE = (HID_REPORT_TYPE_FEATURE << 8) | CONTROLLER_TYPE_REPORT_ID
CONTROLLER_TYPE_REPORT = bytes([
    CONTROLLER_TYPE_REPORT_ID,
    0x21, 0x27, 0x04, 0xc0, 0x07, 0x2c, 0x56,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x0d, 0x0d, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])

HAT_NEUTRAL = 8


def hat_value(up, down, left, right):
    """The hat switch value for a d-pad state. Opposite directions cancel
    each other out."""
    y = down - up
    x = right - left
    return {
        (0, -1): 0, (1, -1): 1, (1, 0): 2, (1, 1): 3,
        (0, 1): 4, (-1, 1): 5, (-1, 0): 6, (-1, -1): 7,
        (0, 0): HAT_NEUTRAL,
    }[(x, y)]


def report_bytes(m, buttons):
    """The input report, as REPORT_LENGTH 8-bit values, for a 16-bit signal
    with PMOD A's buttons in the low byte and PMOD B's in the high one
    (pressed is 1)."""
    up, down, left, right, square, cross, circle, triangle, \
        l1, r1, l2, r2, r3, options, ps, touchpad = buttons

    # A lookup on four bits, giving four bits.
    hat = Signal(4)
    m.d.comb += hat.eq(Array(
        hat_value(n & 1, (n >> 1) & 1, (n >> 2) & 1, (n >> 3) & 1) for n in range(16)
    )[Cat(up, down, left, right)])

    report = [C(0, 8)] * REPORT_LENGTH
    report[0] = C(0x01, 8)                                         # report ID
    report[1] = report[2] = report[3] = report[4] = C(0x80, 8)     # the sticks, centered
    report[5] = Cat(hat, square, cross, circle, triangle)
    report[6] = Cat(l1, r1, l2, r2, C(0), options, C(0), r3)       # no share, L3
    report[7] = Cat(ps, touchpad, C(0, 6))
    report[8] = Mux(l2, 0xFF, 0x00)                                # the triggers' analog values
    report[9] = Mux(r2, 0xFF, 0x00)
    report[35] = report[39] = C(0x80, 8)                           # touchpad: no finger touching
    return report


def create_descriptors():
    descriptors = DeviceDescriptorCollection()

    with descriptors.DeviceDescriptor() as d:
        d.idVendor           = USB_VENDOR_ID
        d.idProduct          = USB_PRODUCT_ID
        d.bcdDevice          = 1.00
        d.iManufacturer      = "Arasaka"
        d.iProduct           = "Zero Latency Gamepad 8K"
        d.bNumConfigurations = 1

    with descriptors.ConfigurationDescriptor() as c:
        c.bMaxPower = 50
        with c.InterfaceDescriptor() as i:
            i.bInterfaceNumber   = 0
            i.bInterfaceClass    = 0x03
            i.bInterfaceSubclass = 0
            i.bInterfaceProtocol = 0

            i.add_subordinate_descriptor(hid_descriptor(len(HID_REPORT_DESCRIPTOR)))

            with i.EndpointDescriptor() as e:
                e.bEndpointAddress = 0x80 | HID_ENDPOINT_NUMBER
                e.bmAttributes     = USBTransferType.INTERRUPT
                e.wMaxPacketSize   = REPORT_LENGTH
                e.bInterval        = 1

    descriptors.add_descriptor(HID_REPORT_DESCRIPTOR, descriptor_type=DESCRIPTOR_TYPE_HID_REPORT)
    add_other_speed_descriptors(descriptors)

    return descriptors


def is_controller_type_request(setup):
    """GET_REPORT of feature report 0x03."""
    return (
        (setup.type == USBRequestType.CLASS)
        & setup.is_in_request
        & (setup.request == HID_REQUEST_GET_REPORT)
        & (setup.value == CONTROLLER_TYPE_REQUEST_VALUE)
    )


class ControllerTypeRequestHandler(ControlRequestHandler):
    """Answers GET_REPORT of feature report 0x03 with CONTROLLER_TYPE_REPORT,
    or as much of it as is asked for."""

    def elaborate(self, platform):
        m = Module()
        interface = self.interface
        setup = interface.setup

        length = len(CONTROLLER_TYPE_REPORT)
        m.submodules.transmitter = transmitter = ConstantStreamGenerator(
            CONTROLLER_TYPE_REPORT, domain="usb", stream_type=USBInStreamInterface,
            max_length_width=length.bit_length())

        with m.If(is_controller_type_request(setup)):
            m.d.comb += [
                interface.claim.eq(1),
                transmitter.stream.attach(interface.tx),
                transmitter.max_length.eq(Mux(setup.length < length, setup.length, length)),
            ]
            with m.If(interface.data_requested):
                m.d.comb += transmitter.start.eq(1)
            with m.If(interface.status_requested):
                m.d.comb += interface.handshakes_out.ack.eq(1)

        return m


class RelayWithInterface:
    """hurra-fpga's control relay as its request handler sees it, except that
    the requests' wIndex isn't the handler's to choose: the console addresses
    our interface 0, the device on TARGET-A may have its HID interface under
    another number."""

    def __init__(self, relay):
        self._relay = relay
        self.index = Signal(16)

    def __getattr__(self, name):
        return getattr(self._relay, name)


class PS4Gamepad(Elaboratable):

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        # The buttons. The platform's own PMOD resources have no pull-ups.
        platform.add_resources([
            Resource("buttons", n, Pins("1 2 3 4 7 8 9 10", conn=("pmod", n), dir="i"),
                     Attrs(IO_TYPE="LVCMOS33", PULLMODE="UP"))
            for n in range(2)
        ])
        pins = Signal(16)
        m.submodules.synchronizer = FFSynchronizer(
            Cat(platform.request("buttons", 0).i, platform.request("buttons", 1).i),
            pins, o_domain="usb", init=0xFFFF)

        # Low is pressed.
        buttons = Signal(16)
        m.d.comb += buttons.eq(~pins)

        # The host, for the device that does the authentication.
        m.submodules.utmi = utmi = UTMITranslator(ulpi=platform.request("target_phy"))
        m.submodules.host = host = BoundedMouseHost(utmi=utmi)
        relay = host.control_relay

        # Everything is powered from AUX, TARGET-A too, switched by the
        # host. The input enables are active low on the board, so o=1 turns
        # them on. (As in hurra-fpga.)
        m.d.comb += [
            platform.request("aux_vbus_in_en").o.eq(1),
            platform.request("control_vbus_in_en").o.eq(0),
            platform.request("aux_vbus_en").o.eq(host.aux_vbus_en),
            platform.request("control_vbus_en").o.eq(0),
            platform.request("target_c_vbus_en").o.eq(0),
            platform.request("target_a_discharge").o.eq(host.target_discharge),
            # Its input reports aren't used.
            host.report_ready.eq(1),
        ]

        # The gamepad.
        m.submodules.usb = usb = USBDevice(bus=platform.request("aux_phy"))

        relay_for_handler = RelayWithInterface(relay)
        m.d.comb += relay.index.eq(host.ep_interface[0])

        control_endpoint = usb.add_standard_control_endpoint(create_descriptors())
        control_endpoint.add_request_handler(HIDClassRequestHandler(
            relay=relay_for_handler, timeout_cycles=RELAY_TIMEOUT_CYCLES, max_packet_size=64,
            skiplist=[is_controller_type_request]))
        control_endpoint.add_request_handler(ControllerTypeRequestHandler())
        control_endpoint.add_request_handler(StallOnlyRequestHandler(
            stall_condition=lambda setup: (setup.type == USBRequestType.VENDOR) | (setup.type == USBRequestType.RESERVED)))

        usb.add_endpoint(LiveReportInEndpoint(
            endpoint_number=HID_ENDPOINT_NUMBER, report=report_bytes(m, buttons)))

        # Without the device on TARGET-A there's nobody to answer the console.
        m.d.comb += usb.connect.eq(host.enumerated)

        # A request the device answered with a STALL didn't fail: that's its
        # answer (to SET_IDLE, often), and the console gets it.
        relay_stalled = Signal()
        with m.If(relay.request_valid & relay.request_ready):
            m.d.usb += relay_stalled.eq(0)
        with m.Elif(relay.ctl_done & relay.engine_owned):
            m.d.usb += relay_stalled.eq(relay.ctl_status == TransactionStatus.STALL.value)
        relay_failed = Signal()
        with m.If(relay.response_valid & relay.response_error & ~relay_stalled):
            m.d.usb += relay_failed.eq(1)

        m.d.comb += [
            platform.request("led", 0).o.eq(host.connected),
            platform.request("led", 1).o.eq(host.enumerated),
            platform.request("led", 2).o.eq(relay.request_pending),
            platform.request("led", 3).o.eq(relay_failed),
            platform.request("led", 4).o.eq(host.error_code != HostError.NONE.value),
            platform.request("led", 5).o.eq(buttons.any()),
        ]

        return m


if __name__ == "__main__":
    top_level_cli(PS4Gamepad)
