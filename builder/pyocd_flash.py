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

"""Flash Intel-hex images to an nRF54L part page-by-page over CMSIS-DAP.

Why this exists instead of a plain `pyocd flash` call
-----------------------------------------------------
pyocd 0.45.1's ``nrf54lm20a`` flash algorithm hardfaults --

    target was not halted as expected after calling flash algorithm
    routine (IPSR=3)

-- whenever more than one page is programmed per flash-algorithm
invocation. Single pages always succeed. ``-O enable_double_buffering=False``
does not help, so the CLI (which batches) cannot be used. This drives
pyocd's low-level flash API one 4096-byte page at a time, with every page
going through a *single* debug session rather than one pyocd process per
page.

``frequency`` matters too: at pyocd's default SWD clock the probe returns
intermittent ``TransferError: SWD/JTAG communication failure (No ACK)``,
especially in the moments right after a chip erase. 1 MHz is reliable.

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
  pyocd_flash.py [--target NAME] [--frequency HZ] [--probe UID] [--erase]
                 [--reset] [--verify] [--python EXE] [file.hex ...]
"""

import os
import subprocess
import sys

PAGE = 0x1000
DEFAULT_TARGET = "nrf54lm20a"
DEFAULT_FREQUENCY = 1000000
PROGRAM_RETRIES = 3
REEXEC_GUARD = "PIO_PYOCD_FLASH_REEXEC"


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
    env_marker = sys.executable

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
        % env_marker)
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


def pages_from(mem):
    """Group a sparse byte map into whole, 0xFF-padded pages.

    0xFF is the erased state of nRF54L RRAM, so padding a partial page
    with it leaves the untouched bytes exactly as the erase left them.
    """
    pages = {}
    for addr, byte in mem.items():
        page = addr & ~(PAGE - 1)
        pages.setdefault(page, bytearray(b"\xff" * PAGE))[addr - page] = byte
    return pages


# ----------------------------------------------------------------- flashing

def group_by_flash(target, addrs):
    """Bucket page addresses by the flash driver that owns them."""
    grouped = {}
    for addr in sorted(addrs):
        region = target.memory_map.get_region_for_address(addr)
        flash = getattr(region, "flash", None) if region else None
        if flash is None:
            raise RuntimeError(
                "no programmable flash region for address 0x%08X - the hex "
                "writes outside anything this pyocd target maps" % addr)
        grouped.setdefault(flash, []).append(addr)
    return grouped


def program_pages(flash, addrs, pages):
    """Erase then program the given pages, one page per algo invocation."""
    flash.init(flash.Operation.ERASE)
    for addr in addrs:
        flash.erase_sector(addr)
    flash.uninit()

    ok = failed = 0
    flash.init(flash.Operation.PROGRAM)
    for addr in addrs:
        for attempt in range(PROGRAM_RETRIES):
            try:
                flash.program_page(addr, bytes(pages[addr]))
                ok += 1
                break
            except Exception as exc:  # pylint: disable=broad-except
                if attempt == PROGRAM_RETRIES - 1:
                    sys.stderr.write("  FAILED 0x%08X: %s\n" % (addr, exc))
                    failed += 1
                else:
                    # A failed algo call leaves the target halted in an
                    # unknown state; re-init before retrying.
                    flash.uninit()
                    flash.init(flash.Operation.PROGRAM)
    flash.uninit()
    return ok, failed


def verify_pages(target, addrs, pages):
    bad = 0
    for addr in addrs:
        got = bytes(target.read_memory_block8(addr, PAGE))
        if got != bytes(pages[addr]):
            sys.stderr.write("  VERIFY FAILED 0x%08X\n" % addr)
            bad += 1
    return bad


def chip_erase(target):
    region = target.memory_map.get_boot_memory()
    flash = region.flash
    flash.init(flash.Operation.ERASE)
    flash.erase_all()
    flash.uninit()


# --------------------------------------------------------------------- main

def parse_args(argv):
    opts = {
        "target": DEFAULT_TARGET,
        "frequency": DEFAULT_FREQUENCY,
        "erase": False,
        "reset": False,
        "verify": False,
        "probe": None,
        "python": None,
        "files": [],
    }
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
        elif arg == "--verify":
            opts["verify"] = True
        elif arg in ("--target", "--frequency", "--probe", "--python"):
            i += 1
            if i >= len(argv):
                raise SystemExit("Error: %s needs a value" % arg)
            value = argv[i]
            key = arg[2:]
            opts[key] = int(value, 0) if key == "frequency" else value
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
        return 1  # only reached when re-exec found nothing

    if not opts["files"] and not opts["erase"]:
        sys.stderr.write("Error: nothing to do - pass a hex file or --erase\n")
        return 1

    # blocking=False: a missing probe must fail the build immediately
    # rather than sit at pyocd's "waiting for a debug probe" prompt.
    session = ConnectHelper.session_with_chosen_probe(
        blocking=False,
        unique_id=opts["probe"],
        target_override=opts["target"],
        options={
            "warning.cortex_m_default": False,
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
            print("Chip erase...")
            chip_erase(target)

        total_ok = total_failed = total_bad = 0
        for path in opts["files"]:
            pages = pages_from(parse_hex(path))
            if not pages:
                sys.stderr.write("Warning: %s contains no data\n" % path)
                continue
            print("%s: %d pages" % (path, len(pages)))
            for flash, addrs in group_by_flash(target, pages).items():
                ok, failed = program_pages(flash, addrs, pages)
                total_ok += ok
                total_failed += failed
                if opts["verify"] and not failed:
                    total_bad += verify_pages(target, addrs, pages)

        print("Programmed %d pages, %d failed%s"
              % (total_ok, total_failed,
                 ", %d mismatched" % total_bad if opts["verify"] else ""))

        if opts["reset"] and not (total_failed or total_bad):
            print("Resetting target")
            target.reset()

    return 1 if (total_failed or total_bad) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
