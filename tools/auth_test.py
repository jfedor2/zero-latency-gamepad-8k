#!/usr/bin/env python3
# Copyright (c) 2026 Jacek Fedorynski
# SPDX-License-Identifier: MIT

"""Plays the console's side of the PS4 controller authentication against the
gamepad, from a Linux PC, to see that the requests reach the device on
TARGET-A and come back, and how long each one takes.

It goes through the motions with a random challenge: it can't check the
signature it gets.

    sudo ./tools/auth_test.py            # finds the gamepad's hidraw device
    sudo ./tools/auth_test.py /dev/hidraw7

Needs root (or access to the hidraw device) and nothing but Python.
"""

import fcntl
import glob
import os
import sys
import time

VENDOR_ID = 0xCAFE
PRODUCT_ID = 0xBAE1

CHUNK = 56
NONCE_CHUNKS = 5
SIGNATURE_CHUNKS = 19
SIGNING_BUSY = 16


def _ioc(direction, number, size):
    return (direction << 30) | (size << 16) | (ord('H') << 8) | number


def set_feature(fd, data):
    fcntl.ioctl(fd, _ioc(3, 0x06, len(data)), bytes(data))


def get_feature(fd, report_id, length):
    buffer = bytearray(length)
    buffer[0] = report_id
    fcntl.ioctl(fd, _ioc(3, 0x07, length), buffer)
    return bytes(buffer)


def find_hidraw():
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = open(os.path.join(path, "device", "uevent")).read()
        except OSError:
            continue
        if f"HID_ID=0003:{VENDOR_ID:08X}:{PRODUCT_ID:08X}" in uevent:
            return "/dev/" + os.path.basename(path)
    sys.exit(f"no hidraw device for {VENDOR_ID:04x}:{PRODUCT_ID:04x} found")


def timed(what, function, *args):
    start = time.monotonic()
    try:
        result = function(*args)
    except OSError as e:
        sys.exit(f"{what}: failed after {(time.monotonic() - start) * 1000:.1f} ms: {e}")
    elapsed = (time.monotonic() - start) * 1000
    return result, elapsed


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else find_hidraw()
    print(f"using {path}")
    fd = os.open(path, os.O_RDWR)

    report, ms = timed("GET 0x03", get_feature, fd, 0x03, 48)
    print(f"GET 0x03 ({ms:.1f} ms): {report.hex(' ')}")

    report, ms = timed("GET 0xF3", get_feature, fd, 0xF3, 8)
    print(f"GET 0xF3 ({ms:.1f} ms): {report.hex(' ')}")

    nonce_id = 1
    nonce = os.urandom(256) + bytes(CHUNK * NONCE_CHUNKS - 256)
    times = []
    for part in range(NONCE_CHUNKS):
        # Report ID, nonce ID, part, 0, the chunk, and where the console has
        # a CRC32, zeros (as HID Remapper sends it).
        report = bytes([0xF0, nonce_id, part, 0]) + nonce[part * CHUNK:(part + 1) * CHUNK] + bytes(4)
        _, ms = timed(f"SET 0xF0 part {part}", set_feature, fd, report)
        times.append(ms)
    print(f"SET 0xF0 x{NONCE_CHUNKS}: " + ", ".join(f"{ms:.1f}" for ms in times) + " ms")

    start = time.monotonic()
    polls = 0
    while True:
        report, ms = timed("GET 0xF2", get_feature, fd, 0xF2, 16)
        polls += 1
        if polls == 1:
            print(f"GET 0xF2 ({ms:.1f} ms): {report.hex(' ')}")
        if report[2] != SIGNING_BUSY:
            break
        if time.monotonic() - start > 30:
            sys.exit(f"still signing after 30 s and {polls} polls: {report.hex(' ')}")
        time.sleep(0.01)
    print(f"signing done after {time.monotonic() - start:.2f} s, {polls} polls: {report.hex(' ')}")
    if report[2] != 0:
        print(f"  (status {report[2]}, not 0: the device didn't like the challenge?)")

    times = []
    signature = b""
    order = []
    for _ in range(SIGNATURE_CHUNKS):
        report, ms = timed("GET 0xF1", get_feature, fd, 0xF1, 64)
        times.append(ms)
        order.append(report[2])
        signature += report[4:4 + CHUNK]
    print(f"GET 0xF1 x{SIGNATURE_CHUNKS}: {min(times):.1f} to {max(times):.1f} ms each")
    print(f"  parts: {order}")
    print(f"  {sum(1 for b in signature if b)} of {len(signature)} bytes nonzero; starts {signature[:16].hex(' ')}")
    if order == list(range(SIGNATURE_CHUNKS)) and any(signature):
        print("OK: the whole exchange went through")
    else:
        print("the response doesn't look right")


if __name__ == "__main__":
    main()
