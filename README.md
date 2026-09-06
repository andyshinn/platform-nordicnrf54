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

## Uploading

The default `upload_protocol` for every board here is `nrfutil` — serial
DFU into the Adafruit-style bootloader from
[caveman99/nRF54_Bootloader](https://github.com/caveman99/nRF54_Bootloader).
That assumes the bootloader is already on the chip.

A **stock Seeed XIAO nRF54LM20A is not in that state**. Out of the box it
enumerates as `Seeed Studio XIAO nRF54LM20A CMSIS-DAP` (VID `0x2886`, PID
`0x0068`) — an onboard debug probe, not a DFU serial port — so
`pio run -t upload` has no port to talk to until the bootloader is
installed once.

To install it, flash the bootloader over SWD using either:

- **An external J-Link / Nordic probe** on the SWD pads:
  `upload_protocol = jlink` or `nrfjprog`. This is the path the nRF54L DKs
  use — they carry an on-board J-Link — and it is unchanged.
- **probe-rs over the onboard CMSIS-DAP probe.** Set

  ```ini
  upload_protocol = cmsis-dap
  ```

  and the `bootloader`, `softdevice`, `erase` and `upload` targets all run
  through probe-rs instead of nrfjprog:

  ```
  pio run -t bootloader     # SoftDevice + bootloader, in one chip erase
  ```

probe-rs must be on `PATH` — install it from <https://probe.rs>. Use
**probe-rs ≥ 0.32**, which lists `nRF54LM20A` in `probe-rs chip list`. It
is not declared as a platform package, the same way `nrfjprog` is not.

`-t bootloader` is deliberately a **single** operation on this path. The
part re-locks debug access on every power cycle while no valid firmware is
running, and regaining access costs an erase-all — so flashing the
bootloader and then the SoftDevice incrementally loses the first write.
The target therefore merges the SoftDevice, the bootloader and the
bootloader settings word into one `bootstrap.hex` with `srec_cat` and
flashes it with a single `--chip-erase`. Use `-t softdevice` on its own
only on a part that is already unlocked and running.

pyocd is not used. Its built-in `nrf54lm20a` target maps no region for the
UICR words the bootloader hex writes at `0x10001014`, so `pyocd flash`
faults partway through the bootloader image.

Once the bootloader is installed, `nrfutil` DFU works normally and is the
intended day-to-day path. `use_1200bps_touch` is `false` for
`xiao_nrf54lm20a`: the SAMD11 owns USB, so touching its CDC port at 1200
baud cannot reset the nRF54LM20A into DFU. Enter DFU with a double-tap of
reset instead.

### Debugging

`cmsis-dap` is the default debug tool for `xiao_nrf54lm20a` and works over
the onboard probe with `pio debug`. Note that OpenOCD can only attach to
and debug code already resident in RRAM — `tool-openocd` ships no nRF54L
flash driver, so it cannot program the part. Use `jlink` with an external
probe if you want to load over the debugger.

## Examples

Three runnable examples live under [`examples/`](examples/):
`arduino-blink`, `arduino-ble-led`, `arduino-ble-uart`. They are also
the integration test matrix for the
[Examples workflow](.github/workflows/examples.yml).

# Configuration

Board JSONs live in [`boards/`](boards/); build flags, upload tooling,
and the framework wiring are in [`builder/`](builder/). For framework
internals see the companion repos' `AGENTS.md` files.
