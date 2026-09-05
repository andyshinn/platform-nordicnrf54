# Nordic nRF54L: development platform for [PlatformIO](https://platformio.org)

[![Build Status](https://github.com/caveman99/platform-nordicnrf54/workflows/Examples/badge.svg)](https://github.com/caveman99/platform-nordicnrf54/actions)

The nRF54L Series features Cortex-M33 processors at 128 MHz with RRAM-based non-volatile memory, providing a highly capable platform for Bluetooth Low Energy applications. Supported MCUs: nRF54L05, nRF54L10, nRF54L15, nRF54LM20A.

This is an out-of-tree PlatformIO platform — it is not published in the
PlatformIO Registry. Install it directly from this repository (see Usage
below). It provides an Adafruit-style Arduino core with the s145
SoftDevice, not Nordic's Zephyr-based SDK.

# Usage

1. [Install PlatformIO](https://platformio.org)
2. Create a PlatformIO project and point `platform` at this repository
   in your [platformio.ini](https://docs.platformio.org/page/projectconf.html):

```ini
[env:development]
platform = https://github.com/caveman99/platform-nordicnrf54.git
board = nrf54l15dk
framework = arduino
```

On first build, PlatformIO pulls the toolchain, the Arduino core from
[caveman99/nRF54_Arduino](https://github.com/caveman99/nRF54_Arduino),
and the DFU bootloader from
[caveman99/nRF54_Bootloader](https://github.com/caveman99/nRF54_Bootloader).

## Boards

| Board ID | MCU |
|---|---|
| `nrf54l15dk` | nRF54L15 |
| `nrf54l10dk` | nRF54L10 |
| `nrf54l05dk` | nRF54L05 |
| `xiao_nrf54l15` | nRF54L15 |
| `xiao_nrf54l15_sense` | nRF54L15 |
| `xiao_nrf54lm20a` | nRF54LM20A |

## Examples

Three runnable examples live under [`examples/`](examples/):
`arduino-blink`, `arduino-ble-led`, `arduino-ble-uart`. They are also
the integration test matrix for the
[Examples workflow](.github/workflows/examples.yml).

# Configuration

Board JSONs live in [`boards/`](boards/); build flags, upload tooling,
and the framework wiring are in [`builder/`](builder/). For framework
internals see the companion repos' `AGENTS.md` files.
