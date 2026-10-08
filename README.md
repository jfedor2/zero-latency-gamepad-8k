# Zero Latency Gamepad 8K

Gateware for the [Cynthion](https://greatscottgadgets.com/cynthion/) FPGA device that lets you use it as an extremely low latency game controller. Low latency is achieved through 8000 Hz reporting rate and reading the button state at the very last moment before the corresponding bits are sent to the host.

There are two versions:

* [gamepad.bit](https://github.com/jfedor2/zero-latency-gamepad-8k/releases/latest/download/gamepad.bit): a controller with 8 generic buttons. The input report is 1 byte long so it takes less time to send it over the wire, for absolute lowest latency.
* [gamepad-ps5.bit](https://github.com/jfedor2/zero-latency-gamepad-8k/releases/latest/download/gamepad-ps5.bit): a controller that you can actually use in fighting games on PS5. It requires an authentication dongle like the MagicBoots PS4 or a licensed PS4 arcade stick plugged into the TARGET-A port. If you've used a controller with the GP2040-CE firmware on the PS5, it's the same deal.

(See [here](https://github.com/jfedor2/zero-latency-gamepad) for a similar project that runs on RP2040/RP2350 boards, but is limited to Full Speed USB (1000 Hz).)

## Loading it onto the Cynthion

You load the gateware the usual way with `apollo flash-program` or `apollo configure` with your computer connected to the CONTROL port.

The game controller device uses the AUX port so connect that to your computer or console to play.

## Wiring

`gamepad.bit` uses the eight pins on the PMOD A header as eight generic buttons. Wire your buttons between the pin and GND.

`gamepad-ps5.bit` uses the following pinout:

pin | 1 | 2 | 3 | 4 | 7 | 8 | 9 | 10
----|---|---|---|---|---|---|---|----
PMOD A | up | down | left | right | square | cross | circle | triangle
PMOD B | L1 | R1 | L2 | R2 | R3 | options | PS | touchpad

## Building

```
git clone https://github.com/jfedor2/zero-latency-gamepad-8k.git
cd zero-latency-gamepad-8k
git submodule update --init
python3 -m venv .virtualenv
. .virtualenv/bin/activate
pip install -r requirements.txt
export LUNA_PLATFORM=cynthion.gateware.platform:CynthionPlatformRev1D4
python -m gateware.top --output gamepad.bit
python -m gateware.ps4 --output gamepad-ps5.bit
```

## Acknowledgements

* Built on top of [LUNA](https://github.com/greatscottgadgets/luna).
* The PS5 version uses the [hurra-fpga](https://github.com/VoltCyclone/hurra-fpga) project for the USB host part.

## License

The software in this repository is licensed under the [MIT License](LICENSE).