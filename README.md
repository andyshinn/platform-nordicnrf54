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

The DK boards and the XIAO nRF54L15 default to `upload_protocol = nrfutil`
— serial DFU into the Adafruit-style bootloader from
[caveman99/nRF54_Bootloader](https://github.com/caveman99/nRF54_Bootloader).
That assumes the bootloader is already on the chip.

A **stock Seeed XIAO nRF54LM20A is not in that state**, and has no way to
get there over USB: out of the box it enumerates only as
`Seeed Studio XIAO nRF54LM20A CMSIS-DAP` (VID `0x2886`, PID `0x0068`) — an
onboard SAMD11 debug probe, not a DFU serial port. It therefore defaults to
`upload_protocol = cmsis-dap`, and everything goes over SWD through that
probe:

```
pio run -t bootloader     # SoftDevice + bootloader + settings, one chip erase
pio run -t upload         # the application (SoftDevice-merged firmware.hex)
```

Run `-t bootloader` once on a fresh board, then `-t upload` for day-to-day
work. The two are deliberately disjoint in RRAM — `-t bootloader` owns
`0x0`, `0x1D0000–0x1D7C48` and the settings page at `0x1D9000`, while
`firmware.hex` covers only `0x1000–<app end>` and the SoftDevice at
`0x1DA800+` — so an upload never disturbs the bootloader.

`-t bootloader` is a **single** operation. The part re-locks debug access on
every power cycle while no valid firmware is running, and regaining access
costs an erase-all — so flashing the bootloader and then the SoftDevice
incrementally loses the first write. The target merges the SoftDevice, the
bootloader and the bootloader settings word (first word `0x00000001`, which
disables the CRC check) into one `bootstrap.hex` with `srec_cat` and flashes
it after a single chip erase. Use `-t softdevice` on its own only on a part
that is already unlocked and running.

### Which tool drives the probe

| `upload_protocol` | Tool | Notes |
|---|---|---|
| `cmsis-dap` (default on `xiao_nrf54lm20a`), `pyocd` | pyocd | Needs `upload.pyocd_target` in the board JSON |
| `probe-rs` | probe-rs | Needs `upload.probe_rs_chip`; probe-rs ≥ 0.32 on `PATH` |
| `jlink`, `nrfjprog` | J-Link / nrfjprog | External probe on the SWD pads; the DK path, unchanged |
| `nrfutil` | adafruit-nrfutil | Serial DFU, once the bootloader is installed |

Neither pyocd nor probe-rs is declared as a platform package, the same way
`nrfjprog` is not.

#### pyocd is driven page-at-a-time, not through its CLI

pyocd 0.45.1's `nrf54lm20a` flash algorithm hardfaults — `target was not
halted as expected after calling flash algorithm routine (IPSR=3)` — as
soon as more than one page is programmed per flash-algorithm invocation.
Single pages always succeed, and `-O enable_double_buffering=False` does not
help, so `pyocd flash` / `pyocd load` cannot be used on this part.

[`builder/pyocd_flash.py`](builder/pyocd_flash.py) works around it: it parses
the hex itself, groups it into 4096-byte pages, and programs them one page
per `program_page()` call (with retries) inside a *single* debug session. It
also pins the SWD clock to 1 MHz — at pyocd's default clock the SAMD11
returns intermittent `SWD/JTAG communication failure (No ACK)`, especially in
the moments right after a chip erase. Override it with
`board_upload.pyocd_frequency` if needed.

The script finds pyocd on its own: `$PYOCD_PYTHON` (or
`board_upload.pyocd_python`), then the interpreter behind any `pyocd` on
`PATH`, then `python3`, and finally `uv run --with pyocd` if
[uv](https://docs.astral.sh/uv/) is installed. `pip install pyocd` anywhere
visible to one of those is enough.

Once the bootloader is installed, `upload_protocol = nrfutil` DFU also works.
`use_1200bps_touch` is `false` for `xiao_nrf54lm20a`: the SAMD11 owns USB, so
touching its CDC port at 1200 baud cannot reset the nRF54LM20A into DFU. Enter
DFU with a double-tap of reset instead.

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
