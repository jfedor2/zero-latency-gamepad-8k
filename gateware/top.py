#!/usr/bin/env python3
# Copyright (c) 2026 Jacek Fedorynski
# SPDX-License-Identifier: MIT

"""Zero latency gamepad for the Cynthion.

A high-speed USB HID gamepad on the AUX port. Its one-byte report is the
state of the eight PMOD A signal pins, read as the report byte goes out.

Build (LUNA_PLATFORM has to be set, see README.md):
    python -m gateware.top --output gamepad.bit

PMOD A pins 1, 2, 3, 4, 7, 8, 9, 10 are buttons 1 to 8. They have pull-ups;
a pin connected to GND is a pressed button.
"""

from amaranth import Signal, Elaboratable, Module
from amaranth.build import Resource, Pins, Attrs
from amaranth.lib.cdc import FFSynchronizer

from usb_protocol.emitters import DeviceDescriptorCollection
from usb_protocol.types import USBRequestType, USBTransferType

from luna import top_level_cli
from luna.usb2 import USBDevice
from luna.gateware.usb.request.control import ControlRequestHandler
from luna.gateware.usb.stream import USBInStreamInterface
from luna.gateware.stream.generator import StreamSerializer

from .endpoint import LiveByteInEndpoint

USB_VENDOR_ID = 0xCAFE
USB_PRODUCT_ID = 0xBAE0

HID_ENDPOINT_NUMBER = 1

DESCRIPTOR_TYPE_DEVICE = 0x01
DESCRIPTOR_TYPE_CONFIGURATION = 0x02
DESCRIPTOR_TYPE_DEVICE_QUALIFIER = 0x06
DESCRIPTOR_TYPE_OTHER_SPEED_CONFIGURATION = 0x07
DESCRIPTOR_TYPE_HID = 0x21
DESCRIPTOR_TYPE_HID_REPORT = 0x22

HID_REQUEST_GET_REPORT = 0x01
HID_REQUEST_SET_IDLE = 0x0A

HID_REPORT_DESCRIPTOR = bytes([
    0x05, 0x01,  # Usage Page (Generic Desktop Ctrls)
    0x09, 0x05,  # Usage (Game Pad)
    0xA1, 0x01,  # Collection (Application)
    0x15, 0x00,  #   Logical Minimum (0)
    0x25, 0x01,  #   Logical Maximum (1)
    0x35, 0x00,  #   Physical Minimum (0)
    0x45, 0x01,  #   Physical Maximum (1)
    0x75, 0x01,  #   Report Size (1)
    0x95, 0x08,  #   Report Count (8)
    0x05, 0x09,  #   Usage Page (Button)
    0x19, 0x01,  #   Usage Minimum (0x01)
    0x29, 0x08,  #   Usage Maximum (0x08)
    0x81, 0x02,  #   Input (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0xC0,        # End Collection
])


def hid_descriptor(report_descriptor_length):
    return bytes([
        9,                           # bLength
        DESCRIPTOR_TYPE_HID,         # bDescriptorType
        0x11, 0x01,                  # bcdHID 1.11
        0,                           # bCountryCode
        1,                           # bNumDescriptors
        DESCRIPTOR_TYPE_HID_REPORT,  # bDescriptorType
        report_descriptor_length & 0xFF, report_descriptor_length >> 8,  # wDescriptorLength
    ])


HID_DESCRIPTOR = hid_descriptor(len(HID_REPORT_DESCRIPTOR))


def add_other_speed_descriptors(descriptors):
    """Adds the two descriptors that say what a high-speed capable device is
    like at the speed it's not running at, to a collection that has its
    device and configuration descriptors. For us that's the same thing: the
    same descriptors serve at full speed and at high speed."""
    device = bytes(descriptors.get_descriptor_bytes(DESCRIPTOR_TYPE_DEVICE))
    configuration = bytes(descriptors.get_descriptor_bytes(DESCRIPTOR_TYPE_CONFIGURATION))

    descriptors.add_descriptor(bytes([
        10,                                # bLength
        DESCRIPTOR_TYPE_DEVICE_QUALIFIER,  # bDescriptorType
        *device[2:8],                      # bcdUSB, bDeviceClass, bDeviceSubClass, bDeviceProtocol, bMaxPacketSize0
        device[17],                        # bNumConfigurations
        0,                                 # bReserved
    ]), descriptor_type=DESCRIPTOR_TYPE_DEVICE_QUALIFIER)

    descriptors.add_descriptor(
        bytes([configuration[0], DESCRIPTOR_TYPE_OTHER_SPEED_CONFIGURATION]) + configuration[2:],
        descriptor_type=DESCRIPTOR_TYPE_OTHER_SPEED_CONFIGURATION)


class HIDRequestHandler(ControlRequestHandler):
    """The class requests: GET_REPORT returns the report, SET_IDLE is
    accepted and ignored. Stalls the rest, and vendor requests too. """

    def __init__(self, report):
        self.report = report
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        interface = self.interface
        setup = interface.setup

        m.submodules.transmitter = transmitter = \
            StreamSerializer(data_length=1, domain="usb", stream_type=USBInStreamInterface, max_length_width=1)

        with m.If(setup.type != USBRequestType.STANDARD):
            m.d.comb += interface.claim.eq(1)

            with m.FSM(domain="usb"):

                with m.State('IDLE'):
                    with m.If(setup.received):
                        m.next = 'UNHANDLED'
                        with m.If(setup.type == USBRequestType.CLASS):
                            with m.Switch(setup.request):
                                with m.Case(HID_REQUEST_GET_REPORT):
                                    m.next = 'GET_REPORT'
                                with m.Case(HID_REQUEST_SET_IDLE):
                                    m.next = 'SET_IDLE'

                with m.State('GET_REPORT'):
                    self.handle_simple_data_request(m, transmitter, self.report, length=1)

                with m.State('SET_IDLE'):
                    with m.If(interface.status_requested):
                        m.d.comb += self.send_zlp()
                    with m.If(interface.handshakes_in.ack):
                        m.next = 'IDLE'

                with m.State('UNHANDLED'):
                    with m.If(interface.data_requested | interface.status_requested):
                        m.d.comb += interface.handshakes_out.stall.eq(1)
                        m.next = 'IDLE'

        return m


def create_descriptors():
    descriptors = DeviceDescriptorCollection()

    with descriptors.DeviceDescriptor() as d:
        d.idVendor           = USB_VENDOR_ID
        d.idProduct          = USB_PRODUCT_ID
        d.iManufacturer      = "Arasaka"
        d.iProduct           = "Zero Latency Gamepad 8K"
        d.bNumConfigurations = 1

    with descriptors.ConfigurationDescriptor() as c:
        with c.InterfaceDescriptor() as i:
            i.bInterfaceNumber   = 0
            i.bInterfaceClass    = 0x03
            i.bInterfaceSubclass = 0
            i.bInterfaceProtocol = 0

            i.add_subordinate_descriptor(HID_DESCRIPTOR)

            with i.EndpointDescriptor() as e:
                e.bEndpointAddress = 0x80 | HID_ENDPOINT_NUMBER
                e.bmAttributes     = USBTransferType.INTERRUPT
                e.wMaxPacketSize   = 1
                # Every microframe at high speed (8000 Hz), every frame
                # at full speed (1000 Hz).
                e.bInterval        = 1

    descriptors.add_descriptor(HID_REPORT_DESCRIPTOR, descriptor_type=DESCRIPTOR_TYPE_HID_REPORT)
    add_other_speed_descriptors(descriptors)

    return descriptors


def add_endpoints(usb, report):
    """Makes a USBDevice the gamepad, sending `report`."""
    control_endpoint = usb.add_standard_control_endpoint(create_descriptors())
    control_endpoint.add_request_handler(HIDRequestHandler(report))

    hid_endpoint = LiveByteInEndpoint(endpoint_number=HID_ENDPOINT_NUMBER, value=report)
    usb.add_endpoint(hid_endpoint)


class ZeroLatencyGamepad(Elaboratable):

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        # The buttons. The platform's own PMOD A resource has no pull-ups.
        platform.add_resources([
            Resource("buttons", 0, Pins("1 2 3 4 7 8 9 10", conn=("pmod", 0), dir="i"),
                     Attrs(IO_TYPE="LVCMOS33", PULLMODE="UP")),
        ])
        pins = Signal(8)
        m.submodules.synchronizer = FFSynchronizer(platform.request("buttons", 0).i, pins, o_domain="usb", init=0xFF)

        # Low is pressed. Nothing but this inversion is between the
        # synchronizer and the transmitter.
        report = Signal(8)
        m.d.comb += report.eq(~pins)

        m.submodules.usb = usb = USBDevice(bus=platform.request("aux_phy"))

        add_endpoints(usb, report)
        m.d.comb += usb.connect.eq(1)

        for n in range(6):
            m.d.comb += platform.request("led", n).o.eq(report[n])

        return m


if __name__ == "__main__":
    top_level_cli(ZeroLatencyGamepad)
