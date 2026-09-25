# Nordic nRF54L: development platform for [PlatformIO](https://platformio.org)

[![Build Status](https://github.com/meshtastic/platform-nordicnrf54/workflows/Examples/badge.svg)](https://github.com/meshtastic/platform-nordicnrf54/actions)

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
platform = https://github.com/meshtastic/platform-nordicnrf54.git
board = nrf54l15dk
framework = arduino
```

On first build, PlatformIO pulls the toolchain, the Arduino core from
[meshtastic/nRF54_Arduino](https://github.com/meshtastic/nRF54_Arduino),
and the DFU bootloader from
[meshtastic/nRF54_Bootloader](https://github.com/meshtastic/nRF54_Bootloader).

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

The DK boards default to `upload_protocol = nrfutil` — serial DFU into the
Adafruit-style bootloader from
[meshtastic/nRF54_Bootloader](https://github.com/meshtastic/nRF54_Bootloader).
That assumes the bootloader is already on the chip.

A **stock Seeed XIAO (nRF54L15, nRF54L15 Sense or nRF54LM20A) is not in that
state**, and has no way to get there over USB: out of the box the USB port
belongs to an onboard SAMD11 CMSIS-DAP probe (the XIAO nRF54LM20A enumerates
as `Seeed Studio XIAO nRF54LM20A CMSIS-DAP`, VID `0x2886`, PID `0x0068`), not
to a DFU serial port. The XIAO boards therefore default to
`upload_protocol = cmsis-dap`, and everything goes over SWD through that
probe:

```
pio run -t bootloader     # SoftDevice + bootloader + settings, one chip erase
pio run -t upload         # the application (SoftDevice-merged firmware.hex)
```

Run `-t bootloader` once on a fresh board, then `-t upload` for day-to-day
work. Neither layout has an MBR:

| RRAM | nRF54LM20A | nRF54L15 |
|---|---|---|
| bootloader | `0x000000–0x008000` | `0x000000–0x008000` |
| application | `0x008000–0x1C9000` | `0x008000–0x147000` |
| LittleFS | `0x1C9000–0x1D1000` | `0x147000–0x14E000` |
| bootloader settings page | `0x1D1000` | `0x14F000` |
| SoftDevice s145 | `0x1DA800+` | `0x15A800+` |

`firmware.hex` carries the bootloader, the application and the SoftDevice,
but nothing in the settings page or LittleFS, so an upload keeps the
settings word `-t bootloader` wrote and leaves the filesystem alone.

`-t bootloader` is a **single** operation. The part re-locks debug access on
every power cycle while no valid firmware is running, so flashing the
bootloader and then the SoftDevice as two separate sessions can lose the
first write. The target merges the SoftDevice, the bootloader and the
bootloader settings word (first word `0x00000001`, which disables the CRC
check) into one `bootstrap.hex` with `srec_cat` and writes that in one
session. Use `-t softdevice` on its own only on a part that is already
unlocked and running.

### Which tool drives the probe

| `upload_protocol` | Tool | Notes |
|---|---|---|
| `cmsis-dap` (default on the XIAO boards), `pyocd` | pyocd | Needs `upload.pyocd_target` and `upload.rramc_base` in the board JSON |
| `probe-rs` | probe-rs | Needs `upload.probe_rs_chip`; probe-rs ≥ 0.32 on `PATH` |
| `jlink`, `nrfjprog` | J-Link / nrfjprog | External probe on the SWD pads; the DK path, unchanged |
| `nrfutil` | adafruit-nrfutil | Serial DFU, once the bootloader is installed |

Neither pyocd nor probe-rs is declared as a platform package, the same way
`nrfjprog` is not.

#### pyocd's flash algorithm is bypassed entirely

**Do not use `pyocd flash` / `pyocd load` on an nRF54L.** pyocd 0.45.1's
flash algorithm for this family silently corrupts **24 bytes at offset
`0x238` of every page it programs**. Writing 4096 bytes of `0xAA` into each
of `0x00000000`, `0x00010000`, `0x001D0000` and `0x001D1000` reads back as
`0xAA` everywhere except a constant window at `+0x238..+0x24F` holding
`01000000 00000000 00000000 00000000 00000000 01000020` — same offset, same
bytes, whatever the page address, and with `cache.enable_memory: False` so it
is not a read-cache artifact. The CPU executes those bytes, which is exactly
how a freshly flashed bootloader HardFaults. `erase_sector()` does not clear
the window. The same broken target support also hardfaults — `target was not
halted as expected after calling flash algorithm routine (IPSR=3)` — as soon
as more than one page is programmed per flash-algorithm invocation.

[`builder/pyocd_flash.py`](builder/pyocd_flash.py) therefore does not touch
the flash algorithm at all. nRF54L non-volatile memory is RRAM, not flash:
it needs no erase and is directly writable over the debug port. The helper
halts the core, sets `RRAMC.CONFIG.WEN`, writes the image straight into RRAM
with `write_memory_block8` in whole 0xFF-padded 4096-byte pages, commits the
RRAMC write buffer, and clears `WEN` again. Register offsets come from
`NRF_RRAMC_Type` in the MDK headers and are the same family-wide; the base
address is not (`0x5004B000` on nRF54L05/10/15, `0x5004E000` on nRF54LM20A),
so it comes from the board JSON's `upload.rramc_base`.

**Read-back verification is mandatory and cannot be turned off.** Every page
written is read back and compared byte for byte, with the session's memory
cache disabled so the reads reach the target; any mismatch fails the target
and skips the reset. A flasher that silently writes the wrong bytes is worse
than one that fails loudly — that is the trap the flash algorithm above set.

The SWD clock is pinned to 1 MHz: at pyocd's default the SAMD11 returns
intermittent `SWD/JTAG communication failure (No ACK)`. Override with
`board_upload.pyocd_frequency` if needed.

`pio run -t erase` still exists and does a debug-port erase-all, but RRAM
needs no erase before being written, so `-t bootloader` and `-t upload` do
not use it. It is only useful for wiping a part or recovering one that has
re-locked its debug access.

The script finds pyocd on its own: `$PYOCD_PYTHON` (or
`board_upload.pyocd_python`), then the interpreter behind any `pyocd` on
`PATH`, then `python3`, and finally `uv run --with pyocd` if
[uv](https://docs.astral.sh/uv/) is installed. `pip install pyocd` anywhere
visible to one of those is enough.

Once the bootloader is installed, `upload_protocol = nrfutil` DFU also works.
`use_1200bps_touch` is `false` on the XIAO boards: the SAMD11 owns USB, so
touching its CDC port at 1200 baud cannot reset the nRF54 into DFU. Enter
DFU with a double-tap of reset instead.

### Debugging

`cmsis-dap` is the default debug tool for the XIAO boards and works over
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
