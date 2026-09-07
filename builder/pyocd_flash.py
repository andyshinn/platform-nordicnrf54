#!/usr/bin/env python3
# Copyright 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Flash Intel-hex images to an nRF54L part over CMSIS-DAP, via RRAMC.

Why this exists instead of a plain `pyocd flash` call
-----------------------------------------------------
pyocd 0.45.1's nRF54L flash algorithm cannot be trusted on this part:

  * It **silently corrupts 24 bytes at offset 0x238 of every page it
    programs.** Programming 4096 bytes of 0xAA into each of 0x00000000,
    0x00010000, 0x001D0000 and 0x001D1000 reads back as 0xAA everywhere
    except a constant window at +0x238..+0x24F holding
    ``01000000 00000000 00000000 00000000 00000000 01000020``. Same offset,
    same bytes, every page, whatever the page address, and with
    ``cache.enable_memory: False`` so it is not a read-cache artifact. The
    CPU executes those bytes, which is what made the bootloader HardFault.
    ``erase_sector()`` does not clear the window either.

  * It also hardfaults - "target was not halted as expected after calling
    flash algorithm routine (IPSR=3)" - as soon as more than one page is
    programmed per flash-algorithm invocation. Same broken target support,
    second symptom.

So the flash algorithm is bypassed completely. nRF54L non-volatile memory
is RRAM, not flash: it needs no erase and is directly writable over the
debug port once RRAMC's CONFIG.WEN is set. This writes the image straight
into RRAM with ``write_memory_block8`` and commits the RRAMC write buffer
at the end.

Register offsets are from ``NRF_RRAMC_Type`` in the framework MDK headers
(identical across the nRF54L family); the *base* is chip-specific -
0x5004B000 on nRF54L05/10/15, 0x5004E000 on nRF54LM20A - so it is passed
in from the board JSON's ``upload.rramc_base`` rather than hardcoded.

Read-back verification is **mandatory and cannot be switched off**: a
flasher that silently writes the wrong bytes is worse than one that fails
loudly, which is exactly the trap the flash algorithm above set. The
session also runs with pyocd's memory cache disabled, so verification
reads reach the target instead of being answered from the cache that just
absorbed the writes.

Finding pyocd
-------------
pyocd is not a PlatformIO package, exactly as nrfjprog and probe-rs are
not. If the interpreter running this script cannot import it, the script
re-executes itself under one that can:

  1. ``$PYOCD_PYTHON`` (or ``--python``), if set
  2. the interpreter behind a ``pyocd`` executable on ``PATH``
  3. ``python3`` / ``python3.13`` / ``python3.12`` / ``python3.11`` on ``PATH``
  4. ``uv run --with pyocd``, if uv is installed

Usage:
  pyocd_flash.py --rramc-base ADDR [--target NAME] [--frequency HZ]
                 [--probe UID] [--page-size N] [--erase] [--reset]
                 [--python EXE] [file.hex ...]
"""

import os
import subprocess
import sys
import time

PAGE = 0x1000
DEFAULT_TARGET = "nrf54lm20a"
DEFAULT_FREQUENCY = 1000000
REEXEC_GUARD = "PIO_PYOCD_FLASH_REEXEC"

# NRF_RRAMC_Type register offsets (nrf54l*_types.h). Identical family-wide.
RRAMC_TASKS_COMMITWRITEBUF = 0x008
RRAMC_READY = 0x400            # bit 0: 1 = ready, 0 = busy
RRAMC_CONFIG = 0x500           # bit 0: WEN, bits 8..13: WRITEBUFSIZE
RRAMC_CONFIG_WEN = 1 << 0
READY_TIMEOUT_S = 2.0
MAX_DIFFS_REPORTED = 10


# ---------------------------------------------------------------- bootstrap

def _has_pyocd(executable):
    try:
        return subprocess.call(
            [executable, "-c", "import pyocd"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def _shebang_interpreter(script):
    """The interpreter a console-script wrapper was generated for."""
    try:
        with open(script, "rb") as fp:
            first = fp.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    exe = first[2:].strip().decode("utf-8", "replace")
    # "#!/usr/bin/env python3" hands us no concrete path.
    if not exe.startswith("/") or " " in exe:
        return None
    return exe if os.path.isfile(exe) else None


def _candidate_interpreters(explicit):
    from shutil import which

    seen = set()
    for cand in (explicit, os.environ.get("PYOCD_PYTHON")):
        if cand:
            yield cand
            seen.add(cand)

    pyocd_cli = which("pyocd")
    if pyocd_cli:
        exe = _shebang_interpreter(pyocd_cli)
        if exe and exe not in seen:
            seen.add(exe)
            yield exe

    for name in ("python3", "python3.13", "python3.12", "python3.11"):
        exe = which(name)
        if exe and exe not in seen:
            seen.add(exe)
            yield exe


def reexec_with_pyocd(argv, explicit_python):
    """Re-run this script under an interpreter that can import pyocd."""
    from shutil import which

    if os.environ.get(REEXEC_GUARD):
        sys.stderr.write(
            "Error: %s cannot import pyocd.\n" % os.environ[REEXEC_GUARD])
        return None

    for exe in _candidate_interpreters(explicit_python):
        if _has_pyocd(exe):
            os.environ[REEXEC_GUARD] = exe
            print("Using pyocd from %s" % exe)
            sys.stdout.flush()
            os.execv(exe, [exe, os.path.abspath(__file__)] + argv)

    uv = which("uv")
    if uv:
        print("No local pyocd found; running it through uv")
        os.environ[REEXEC_GUARD] = "uv run --with pyocd"
        cmd = [uv, "run", "--no-project", "--quiet", "--python", "3.12",
               "--with", "pyocd>=0.45.1", "python",
               os.path.abspath(__file__)] + argv
        sys.stdout.flush()
        os.execv(uv, cmd)

    sys.stderr.write(
        "Error: pyocd is required to flash over CMSIS-DAP but no Python with\n"
        "       it installed was found (tried %s, a `pyocd` on PATH, python3,\n"
        "       and `uv run`).\n"
        "       Install it with `pip install pyocd`, or point PYOCD_PYTHON /\n"
        "       board_upload.pyocd_python at an interpreter that has it.\n"
        % sys.executable)
    return None


# ------------------------------------------------------------------ hex I/O

def parse_hex(path):
    """Return a {address: byte} sparse map for an Intel hex file."""
    base = 0
    out = {}
    with open(path) as fp:
        for line in fp:
            line = line.strip()
            if not line.startswith(":"):
                continue
            count = int(line[1:3], 16)
            addr = int(line[3:7], 16)
            rectype = int(line[7:9], 16)
            if rectype == 4:  # extended linear address
                base = int(line[9:13], 16) << 16
            elif rectype == 2:  # extended segment address
                base = int(line[9:13], 16) << 4
            elif rectype == 0:
                data = bytes.fromhex(line[9:9 + count * 2])
                for i, byte in enumerate(data):
                    out[base + addr + i] = byte
    return out


def add_pages(pages, mem, page_size):
    """Merge a sparse byte map into whole, 0xFF-padded pages.

    Writing whole pages is what makes the missing erase step a non-issue:
    every byte of a page the image touches is written, so stale content
    cannot survive underneath. 0xFF is the value an erase would have left.
    """
    for addr, byte in mem.items():
        page = addr & ~(page_size - 1)
        pages.setdefault(
            page, bytearray(b"\xff" * page_size))[addr - page] = byte
    return pages


# ----------------------------------------------------------------- flashing

def wait_ready(target, ready_reg):
    deadline = time.time() + READY_TIMEOUT_S
    while time.time() < deadline:
        if target.read32(ready_reg) & 1:
            return True
        time.sleep(0.001)
    return False


def write_pages(target, rramc_base, pages):
    """Write every page straight into RRAM, then commit the write buffer."""
    config = rramc_base + RRAMC_CONFIG
    target.write32(config, RRAMC_CONFIG_WEN)
    try:
        for addr in sorted(pages):
            target.write_memory_block8(addr, bytes(pages[addr]))
        # WRITEBUFSIZE is left at 0 (unbuffered) by the CONFIG write above,
        # so this is a belt-and-braces flush of anything still pending.
        target.write32(rramc_base + RRAMC_TASKS_COMMITWRITEBUF, 1)
        return wait_ready(target, rramc_base + RRAMC_READY)
    finally:
        target.write32(config, 0)


def verify_pages(target, pages, page_size):
    """Read every written page back and compare. Returns bad byte count."""
    bad = 0
    for addr in sorted(pages):
        expected = bytes(pages[addr])
        got = bytes(target.read_memory_block8(addr, page_size))
        if got == expected:
            continue
        for i in range(page_size):
            if got[i] != expected[i]:
                if bad < MAX_DIFFS_REPORTED:
                    sys.stderr.write(
                        "  MISMATCH 0x%08X: read %02X, expected %02X\n"
                        % (addr + i, got[i], expected[i]))
                bad += 1
    return bad


def mass_erase(target):
    """Erase the whole part.

    RRAM needs no erase before a write, so this is only useful to wipe a
    part or to recover one that has re-locked its debug access. It goes
    through the debug port's erase-all, not the flash algorithm - pyocd's
    `erase_all` leaves the corrupt +0x238 window behind.
    """
    return target.mass_erase()


# --------------------------------------------------------------------- main

def parse_args(argv):
    opts = {
        "target": DEFAULT_TARGET,
        "frequency": DEFAULT_FREQUENCY,
        "rramc-base": None,
        "page-size": PAGE,
        "erase": False,
        "reset": False,
        "probe": None,
        "python": None,
        "files": [],
    }
    valued = ("--target", "--frequency", "--rramc-base", "--page-size",
              "--probe", "--python")
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        if arg == "--erase":
            opts["erase"] = True
        elif arg == "--reset":
            opts["reset"] = True
        elif arg in valued:
            i += 1
            if i >= len(argv):
                raise SystemExit("Error: %s needs a value" % arg)
            key = arg[2:]
            value = argv[i]
            opts[key] = (int(value, 0)
                         if key in ("frequency", "rramc-base", "page-size")
                         else value)
        elif arg.startswith("-"):
            raise SystemExit("Error: unknown option %s" % arg)
        else:
            opts["files"].append(arg)
        i += 1
    return opts


def main(argv):
    opts = parse_args(argv)

    try:
        from pyocd.core.helpers import ConnectHelper
    except ImportError:
        reexec_with_pyocd(argv, opts["python"])
        return 1  # only reached when the re-exec search found nothing

    if not opts["files"] and not opts["erase"]:
        sys.stderr.write("Error: nothing to do - pass a hex file or --erase\n")
        return 1
    if opts["files"] and opts["rramc-base"] is None:
        sys.stderr.write(
            "Error: --rramc-base is required to write RRAM (0x5004B000 on "
            "nRF54L05/10/15, 0x5004E000 on nRF54LM20A).\n")
        return 1

    page_size = opts["page-size"]
    pages = {}
    for path in opts["files"]:
        before = len(pages)
        add_pages(pages, parse_hex(path), page_size)
        print("%s: %d pages" % (path, len(pages) - before))
    if opts["files"] and not pages:
        sys.stderr.write("Error: the given hex files contain no data\n")
        return 1

    # blocking=False: a missing probe must fail the build immediately rather
    # than sit at pyocd's "waiting for a debug probe" prompt.
    # cache.enable_memory=False: verification has to read the target, not the
    # cache that just absorbed the writes.
    session = ConnectHelper.session_with_chosen_probe(
        blocking=False,
        unique_id=opts["probe"],
        target_override=opts["target"],
        options={
            "warning.cortex_m_default": False,
            "cache.enable_memory": False,
            "frequency": opts["frequency"],
        })
    if session is None:
        sys.stderr.write(
            "Error: no CMSIS-DAP probe found. The XIAO nRF54LM20A's onboard "
            "probe enumerates as USB 2886:0068.\n")
        return 1

    with session:
        target = session.target
        target.reset_and_halt()

        if opts["erase"]:
            print("Erasing the whole part...")
            mass_erase(target)
            target.reset_and_halt()

        if not pages:
            return 0

        print("Writing %d pages to RRAM..." % len(pages))
        if not write_pages(target, opts["rramc-base"], pages):
            sys.stderr.write(
                "Error: RRAMC did not report ready after the commit\n")
            return 1

        print("Verifying %d pages..." % len(pages))
        bad = verify_pages(target, pages, page_size)
        if bad:
            sys.stderr.write(
                "Error: %d byte(s) read back wrong - the part was NOT "
                "programmed correctly\n" % bad)
            return 1
        print("OK: %d pages written and verified" % len(pages))

        if opts["reset"]:
            print("Resetting target")
            target.reset()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
