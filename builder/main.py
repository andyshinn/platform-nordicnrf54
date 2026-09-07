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

import sys
from platform import system
from os import makedirs
from os.path import isdir, join

from SCons.Script import (ARGUMENTS, COMMAND_LINE_TARGETS, AlwaysBuild,
                          Builder, Default, DefaultEnvironment)

from platformio.public import list_serial_ports


def BeforeUpload(target, source, env):  # pylint: disable=W0613,W0621
    env.AutodetectUploadPort()

    upload_options = {}
    if "BOARD" in env:
        upload_options = env.BoardConfig().get("upload", {})

    if not bool(upload_options.get("disable_flushing", False)):
        env.FlushSerialBuffer("$UPLOAD_PORT")

    before_ports = list_serial_ports()

    if bool(upload_options.get("use_1200bps_touch", False)):
        env.TouchSerialPort("$UPLOAD_PORT", 1200)

    if bool(upload_options.get("wait_for_upload_port", False)):
        env.Replace(UPLOAD_PORT=env.WaitForNewSerialPort(before_ports))


env = DefaultEnvironment()
platform = env.PioPlatform()
board = env.BoardConfig()
variant = board.get("build.variant", "")

upload_protocol = env.subst("$UPLOAD_PROTOCOL")

SRECCAT = join(platform.get_package_dir("tool-sreccat") or "", "srec_cat")

# nrfjprog drives a J-Link and nothing else. That is right for the DKs, which
# carry an on-board J-Link, so the nrfjprog path below is left untouched. It
# is useless on the XIAO nRF54LM20A, whose only probe is an on-board SAMD11
# CMSIS-DAP bridge (USB 2886:0068). Those boards run the same operations
# through a tool that speaks CMSIS-DAP:
#
#   * pyocd - selected by "cmsis-dap" (the default for such boards) and by
#     "pyocd". Driven through builder/pyocd_flash.py rather than the `pyocd`
#     CLI: pyocd 0.45.1's nRF54L flash algorithm silently corrupts 24 bytes
#     of every page it programs (and hardfaults on multi-page writes), so the
#     helper bypasses it and writes RRAM directly through RRAMC, verifying
#     every byte it wrote. It finds pyocd itself (see that file's header).
#
#   * probe-rs - selected by "probe-rs". Expected on PATH, exactly as this
#     file already invokes nrfjprog. Use probe-rs >= 0.32, which lists
#     nRF54L15 and nRF54LM20A in `probe-rs chip list`.
use_pyocd = upload_protocol in ("cmsis-dap", "pyocd")
use_probe_rs = upload_protocol == "probe-rs"
use_swd_probe = use_pyocd or use_probe_rs

# Both tools name parts their own way - probe-rs as Nordic does
# ("nRF54LM20A"), pyocd in lower case ("nrf54lm20a") - and neither matches
# build.mcu ("nrf54lm20") or the variant, so boards wired to a CMSIS-DAP
# probe declare the names explicitly.
probe_rs_chip = board.get("upload.probe_rs_chip", "")
pyocd_target = board.get("upload.pyocd_target", "")
# RRAMC base address, from NRF_RRAMC_S_BASE in the MDK headers: 0x5004B000
# on nRF54L05/10/15, 0x5004E000 on nRF54LM20A. The helper writes RRAM
# through this peripheral instead of pyocd's broken flash algorithm.
rramc_base = board.get("upload.rramc_base", "")
# At pyocd's default SWD clock the onboard SAMD11 returns intermittent
# "No ACK" transfer errors, especially right after a chip erase. 1 MHz is
# reliable; overridable per board via upload.pyocd_frequency.
pyocd_frequency = str(board.get("upload.pyocd_frequency", 1000000))
# Optional escape hatch: an interpreter that has pyocd installed, for setups
# where the helper's own search would not find one.
pyocd_python = board.get("upload.pyocd_python", "")

PYOCD_FLASH = join(platform.get_dir(), "builder", "pyocd_flash.py")

PROBE_TARGETS = set(["upload", "bootloader", "softdevice", "erase"])
probe_target_requested = bool(PROBE_TARGETS & set(COMMAND_LINE_TARGETS))

if use_probe_rs and not probe_rs_chip and probe_target_requested:
    sys.stderr.write(
        "Error. Board '%s' selects upload_protocol '%s' but declares no "
        "upload.probe_rs_chip, so probe-rs has no chip to target.\n"
        % (env.subst("$BOARD"), upload_protocol))
    env.Exit(1)

if use_pyocd and probe_target_requested:
    for key, value in (("upload.pyocd_target", pyocd_target),
                       ("upload.rramc_base", rramc_base)):
        if not value:
            sys.stderr.write(
                "Error. Board '%s' selects upload_protocol '%s' but declares "
                "no %s, which the pyocd flasher needs.\n"
                % (env.subst("$BOARD"), upload_protocol, key))
            env.Exit(1)


def ProbeRsCmd(subcommand, *args):
    """A probe-rs invocation carrying the board's chip selection."""
    return " ".join(
        ["probe-rs", subcommand, "--chip", probe_rs_chip] + list(args))


def PyocdCmd(*args):
    """A builder/pyocd_flash.py invocation for this board."""
    cmd = ['"$PYTHONEXE"', '"%s"' % PYOCD_FLASH,
           "--target", pyocd_target,
           "--rramc-base", str(rramc_base),
           "--frequency", pyocd_frequency]
    if pyocd_python:
        cmd += ["--python", '"%s"' % pyocd_python]
    return " ".join(cmd + list(args))


env.Replace(
    AR="arm-none-eabi-ar",
    AS="arm-none-eabi-as",
    CC="arm-none-eabi-gcc",
    CXX="arm-none-eabi-g++",
    GDB="arm-none-eabi-gdb",
    OBJCOPY="arm-none-eabi-objcopy",
    RANLIB="arm-none-eabi-ranlib",
    SIZETOOL="arm-none-eabi-size",

    ARFLAGS=["rc"],

    SIZEPROGREGEXP=r"^(?:\.text|\.data|\.rodata|\.text.align|\.ARM.exidx)\s+(\d+).*",
    SIZEDATAREGEXP=r"^(?:\.data|\.bss|\.noinit)\s+(\d+).*",
    SIZECHECKCMD="$SIZETOOL -A -d $SOURCES",
    SIZEPRINTCMD='$SIZETOOL -B -d $SOURCES',

    ERASEFLAGS=([] if use_swd_probe else ["--eraseall", "-f", "nrf54l"]),
    ERASECMD=(PyocdCmd("--erase") if use_pyocd
              else ProbeRsCmd("erase") if use_probe_rs
              else "nrfjprog $ERASEFLAGS"),

    PROGSUFFIX=".elf"
)

# Allow user to override via pre:script
if env.get("PROGNAME", "program") == "program":
    env.Replace(PROGNAME="firmware")

env.Append(
    BUILDERS=dict(
        ElfToBin=Builder(
            action=env.VerboseAction(" ".join([
                "$OBJCOPY",
                "-O",
                "binary",
                "$SOURCES",
                "$TARGET"
            ]), "Building $TARGET"),
            suffix=".bin"
        ),
        ElfToHex=Builder(
            action=env.VerboseAction(" ".join([
                "$OBJCOPY",
                "-O",
                "ihex",
                "-R",
                ".eeprom",
                "$SOURCES",
                "$TARGET"
            ]), "Building $TARGET"),
            suffix=".hex"
        ),
        MergeHex=Builder(
            action=env.VerboseAction(" ".join([
                '"%s"' % SRECCAT,
                "$SOFTDEVICEHEX",
                "-intel",
                "$SOURCES",
                "-intel",
                "-o",
                "$TARGET",
                "-intel",
                "--line-length=44"
            ]), "Building $TARGET"),
            suffix=".hex"
        )
    )
)

if "nrfutil" == upload_protocol or (
    board.get("build.bsp.name", "") == "adafruit"
    and "arduino" in env.get("PIOFRAMEWORK", [])
):
    env.Append(
        BUILDERS=dict(
            PackageDfu=Builder(
                action=env.VerboseAction(" ".join([
                    '"$PYTHONEXE"',
                    '"%s"' % join(platform.get_package_dir(
                        "tool-adafruit-nrfutil") or "", "adafruit-nrfutil.py"),
                    "dfu",
                    "genpkg",
                    "--dev-type",
                    "0x0052",
                    "--sd-req",
                    board.get("build.softdevice.sd_fwid"),
                    "--application",
                    "$SOURCES",
                    "$TARGET"
                ]), "Building $TARGET"),
                suffix=".zip"
            ),
            SignBin=Builder(
                action=env.VerboseAction(
                    " ".join(
                        [
                            '"$PYTHONEXE"',
                            '"%s"' % join(
                                platform.get_package_dir(
                                    "framework-arduinoadafruitnrf54"
                                )
                                or "",
                                "tools",
                                "pynrfbintool",
                                "pynrfbintool.py",
                            ),
                            "--signature",
                            "$TARGET",
                            "$SOURCES",
                        ]
                    ),
                    "Signing $SOURCES",
                ),
                suffix="_signature.bin",
            ),
        )
    )


if not env.get("PIOFRAMEWORK"):
    env.SConscript("frameworks/_bare.py")

target_elf = None
if "nobuild" in COMMAND_LINE_TARGETS:
    target_elf = join("$BUILD_DIR", "${PROGNAME}.elf")
    target_firm = join("$BUILD_DIR", "${PROGNAME}.hex")
else:
    target_elf = env.BuildProgram()

    if "SOFTDEVICEHEX" in env:
        target_firm = env.MergeHex(
            join("$BUILD_DIR", "${PROGNAME}"),
            env.ElfToHex(join("$BUILD_DIR", "userfirmware"), target_elf))
    elif "nrfutil" == upload_protocol:
        target_firm = env.PackageDfu(
            join("$BUILD_DIR", "${PROGNAME}"),
            env.ElfToHex(join("$BUILD_DIR", "${PROGNAME}"), target_elf))
    elif "nrfjprog" == upload_protocol:
        target_firm = env.ElfToHex(
            join("$BUILD_DIR", "${PROGNAME}"), target_elf)
    else:
        if "DFUBOOTHEX" in env:
            target_firm = env.SignBin(
                join("$BUILD_DIR", "${PROGNAME}"),
                env.ElfToBin(join("$BUILD_DIR", "${PROGNAME}"), target_elf))
        else:
            target_firm = env.ElfToHex(
                join("$BUILD_DIR", "${PROGNAME}"), target_elf)
        env.Depends(target_firm, "checkprogsize")

AlwaysBuild(env.Alias("nobuild", target_firm))
target_buildprog = env.Alias("buildprog", target_firm, target_firm)

target_dfu = None

if "DFUBOOTHEX" in env:
    env.Append(
        BOOT_SETTING_ADDR=board.get("build.bootloader.settings_addr", "0x7F000")
    )

    # The DFU package wraps the plain application hex. When a SoftDevice
    # is present, that hex is userfirmware.hex (firmware.hex is the
    # SD-merged image built above) - reusing the same ElfToHex target
    # avoids a "multiple ways to build firmware.hex" SCons collision.
    # Without a SoftDevice the app hex is just ${PROGNAME}.hex.
    if "SOFTDEVICEHEX" in env:
        dfu_app_hex = env.ElfToHex(join("$BUILD_DIR", "userfirmware"), target_elf)
    else:
        dfu_app_hex = env.ElfToHex(join("$BUILD_DIR", "${PROGNAME}"), target_elf)

    target_dfu = env.PackageDfu(
        join("$BUILD_DIR", "${PROGNAME}"),
        dfu_app_hex,
    )

    env.AddPlatformTarget(
        "dfu",
        target_dfu,
        target_firm,
        "Generate DFU Image",
    )

    if use_swd_probe:
        # The part re-locks debug access on every power cycle while no valid
        # firmware is running, and regaining access costs an erase-all. So
        # this is one shot: merge the SoftDevice, the bootloader and the
        # bootloader settings word into a single image and flash it in a
        # single debug session, rather than the incremental
        # bootloader-then-softdevice dance the nrfjprog path uses.
        settings_addr = int(
            board.get("build.bootloader.settings_addr", "0x7F000"), 16)
        bootstrap_hex = join("$BUILD_DIR", "bootstrap.hex")

        merge_cmd = ['"%s"' % SRECCAT]
        if "SOFTDEVICEHEX" in env:
            merge_cmd += ["$SOFTDEVICEHEX", "-intel"]
        merge_cmd += [
            "$DFUBOOTHEX", "-intel",
            # Bootloader settings: first word set to 1 disables the CRC check
            # so the bootloader accepts the application handed to it later.
            # The rest of that page ends up 0xFF either way - the pyocd
            # helper writes whole 0xFF-padded pages, the probe-rs path
            # chip-erases first.
            "-generate", hex(settings_addr), hex(settings_addr + 4),
            "-constant-l-e", "0x00000001", "4",
            "-o", bootstrap_hex, "-intel", "--line-length=44",
        ]

        if use_pyocd:
            # No erase: RRAM is directly writable, and the helper writes
            # whole pages, so nothing stale survives under the image. An
            # erase-all here would only cost time (and pyocd's own erase
            # goes through the flash algorithm this path exists to avoid).
            flash_bootstrap = [
                env.VerboseAction(
                    PyocdCmd("--reset", '"%s"' % bootstrap_hex),
                    "Flashing SoftDevice + bootloader",
                ),
            ]
        else:
            flash_bootstrap = [
                env.VerboseAction(
                    ProbeRsCmd("download", "--binary-format", "hex",
                               "--allow-erase-all", "--chip-erase",
                               bootstrap_hex),
                    "Flashing SoftDevice + bootloader",
                ),
                env.VerboseAction(ProbeRsCmd("reset"), "Reset nRF54L"),
            ]

        bootloader_actions = [
            env.VerboseAction(" ".join(merge_cmd), "Building %s" % bootstrap_hex),
        ] + flash_bootstrap
    else:
        bootloader_actions = [
            env.VerboseAction(
                "nrfjprog --program $DFUBOOTHEX -f nrf54l --chiperase",
                "Uploading $DFUBOOTHEX",
            ),
            env.VerboseAction(
                "nrfjprog --erasepage $BOOT_SETTING_ADDR -f nrf54l",
                "Erasing bootloader config",
            ),
            env.VerboseAction(
                "nrfjprog --memwr $BOOT_SETTING_ADDR --val 0x00000001 -f nrf54l",
                "Disable CRC check",
            ),
            env.VerboseAction("nrfjprog --reset -f nrf54l", "Reset nRF54L"),
        ]

    env.AddPlatformTarget(
        "bootloader", None, bootloader_actions, "Burn Bootloader")

if "bootloader" in COMMAND_LINE_TARGETS and "DFUBOOTHEX" not in env:
    sys.stderr.write("Error. The board is missing the bootloader binary.\n")
    env.Exit(1)

if "SOFTDEVICEHEX" in env:
    if use_pyocd:
        # Programs the SoftDevice on its own, leaving the rest of RRAM alone.
        # On a stock (debug-locked) part this cannot work -- regaining access
        # needs an erase-all -- so use the `bootloader` target there, which
        # flashes the SoftDevice and the bootloader together in one pass.
        softdevice_actions = [
            env.VerboseAction(
                PyocdCmd("--reset", '"$SOFTDEVICEHEX"'),
                "Flashing SoftDevice $SOFTDEVICEHEX",
            ),
        ]
    elif use_probe_rs:
        softdevice_actions = [
            env.VerboseAction(
                ProbeRsCmd("download", "--binary-format", "hex",
                           "$SOFTDEVICEHEX"),
                "Flashing SoftDevice $SOFTDEVICEHEX",
            ),
            env.VerboseAction(ProbeRsCmd("reset"), "Reset nRF54L"),
        ]
    else:
        softdevice_actions = [
            env.VerboseAction(
                "nrfjprog --program $SOFTDEVICEHEX -f nrf54l --sectorerase --verify",
                "Flashing SoftDevice $SOFTDEVICEHEX",
            ),
            env.VerboseAction("nrfjprog --reset -f nrf54l", "Reset nRF54L"),
        ]

    env.AddPlatformTarget(
        "softdevice", None, softdevice_actions, "Flash SoftDevice")

if "softdevice" in COMMAND_LINE_TARGETS and "SOFTDEVICEHEX" not in env:
    sys.stderr.write("Error. The board is missing the SoftDevice binary.\n")
    env.Exit(1)

target_size = env.AddPlatformTarget(
    "size",
    target_elf,
    env.VerboseAction("$SIZEPRINTCMD", "Calculating size $SOURCE"),
    "Program Size",
    "Calculate program size",
)

debug_tools = env.BoardConfig().get("debug.tools", {})
upload_actions = []

if upload_protocol == "mbed":
    upload_actions = [
        env.VerboseAction(env.AutodetectUploadPort, "Looking for upload disk..."),
        env.VerboseAction(env.UploadToDisk, "Uploading $SOURCE")
    ]

elif upload_protocol.startswith("blackmagic"):
    env.Replace(
        UPLOADER="$GDB",
        UPLOADERFLAGS=[
            "-nx",
            "--batch",
            "-ex", "target extended-remote $UPLOAD_PORT",
            "-ex", "monitor %s_scan" %
            ("jtag" if upload_protocol == "blackmagic-jtag" else "swdp"),
            "-ex", "attach 1",
            "-ex", "load",
            "-ex", "compare-sections",
            "-ex", "kill"
        ],
        UPLOADCMD="$UPLOADER $UPLOADERFLAGS $BUILD_DIR/${PROGNAME}.elf"
    )
    upload_actions = [
        env.VerboseAction(env.AutodetectUploadPort, "Looking for BlackMagic port..."),
        env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")
    ]

elif upload_protocol == "nrfjprog":
    env.Replace(
        UPLOADER="nrfjprog",
        UPLOADERFLAGS=[
            "--sectorerase" if "DFUBOOTHEX" in env else "--chiperase",
            "--reset"
        ],
        UPLOADCMD="$UPLOADER $UPLOADERFLAGS --program $SOURCE"
    )
    upload_actions = [env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")]

elif upload_protocol == "nrfutil":
    env.Replace(
        UPLOADER=join(platform.get_package_dir(
            "tool-adafruit-nrfutil") or "", "adafruit-nrfutil.py"),
        UPLOADERFLAGS=[
            "dfu",
            "serial",
            "-p",
            "$UPLOAD_PORT",
            "-b",
            "$UPLOAD_SPEED",
            "--singlebank",
        ],
        UPLOADCMD='"$PYTHONEXE" "$UPLOADER" $UPLOADERFLAGS -pkg $SOURCE'
    )
    upload_actions = [
        env.VerboseAction(BeforeUpload, "Looking for upload port..."),
        env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")
    ]

elif upload_protocol.startswith("jlink"):

    def _jlink_cmd_script(env, source):
        build_dir = env.subst("$BUILD_DIR")
        if not isdir(build_dir):
            makedirs(build_dir)
        script_path = join(build_dir, "upload.jlink")
        commands = ["h"]
        if "DFUBOOTHEX" in env:
            commands.append('loadbin "%s",%s' % (str(source).replace("_signature", ""),
                env.BoardConfig().get("upload.offset_address", "0x26000")))
            commands.append('loadbin "%s",%s' % (source, env.get("BOOT_SETTING_ADDR")))
        else:
            commands.append('loadbin "%s",%s' % (source, env.BoardConfig().get(
                "upload.offset_address", "0x0")))

        commands.append("r")
        commands.append("q")

        with open(script_path, "w") as fp:
            fp.write("\n".join(commands))
        return script_path

    env.Replace(
        __jlink_cmd_script=_jlink_cmd_script,
        UPLOADER="JLink.exe" if system() == "Windows" else "JLinkExe",
        UPLOADERFLAGS=[
            "-device", env.BoardConfig().get("debug", {}).get("jlink_device"),
            "-speed", env.GetProjectOption("debug_speed", "4000"),
            "-if", ("jtag" if upload_protocol == "jlink-jtag" else "swd"),
            "-autoconnect", "1",
            "-NoGui", "1"
        ],
        UPLOADCMD='$UPLOADER $UPLOADERFLAGS -CommanderScript "${__jlink_cmd_script(__env__, SOURCE)}"'
    )
    upload_actions = [env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")]

elif use_swd_probe:
    # Ordered ahead of the debug_tools branch below: platform.py registers a
    # "cmsis-dap" OpenOCD debug tool for any board listing that protocol, and
    # tool-openocd ships no nRF54L flash driver, so it cannot program the part.
    if use_pyocd:
        # $SOURCE is the SoftDevice-merged firmware.hex. It leaves the
        # bootloader (0x0 and 0x1D0000+) and the settings page untouched -
        # nothing in the image lands in those pages - so an upload onto a
        # part prepared with `-t bootloader` keeps it bootable.
        env.Replace(UPLOADCMD=PyocdCmd("--reset", '"$SOURCE"'))
        upload_actions = [
            env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE"),
        ]
    else:
        env.Replace(
            UPLOADER="probe-rs",
            UPLOADERFLAGS=[
                "download",
                "--chip", probe_rs_chip,
                "--binary-format", "hex",
            ],
            UPLOADCMD="$UPLOADER $UPLOADERFLAGS $SOURCE",
        )
        upload_actions = [
            env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE"),
            env.VerboseAction(ProbeRsCmd("reset"), "Reset nRF54L"),
        ]

elif upload_protocol in debug_tools:
    openocd_args = [
        "-d%d" % (2 if int(ARGUMENTS.get("PIOVERBOSE", 0)) else 1)
    ]
    openocd_args.extend(
        debug_tools.get(upload_protocol).get("server").get("arguments", []))
    if env.GetProjectOption("debug_speed"):
        openocd_args.extend(
            ["-c", "adapter speed %s" % env.GetProjectOption("debug_speed")]
        )
    openocd_args.extend([
        "-c", "program {$SOURCE} %s verify reset; shutdown;" %
        board.get("upload.offset_address", "")
    ])
    openocd_args = [
        f.replace("$PACKAGE_DIR",
                  platform.get_package_dir("tool-openocd") or "")
        for f in openocd_args
    ]
    env.Replace(
        UPLOADER="openocd",
        UPLOADERFLAGS=openocd_args,
        UPLOADCMD="$UPLOADER $UPLOADERFLAGS")
    upload_actions = [env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")]

elif upload_protocol == "custom":
    upload_actions = [env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")]

else:
    sys.stderr.write("Warning! Unknown upload protocol %s\n" % upload_protocol)

# adafruit-nrfutil's serial DFU consumes the .zip package, not a raw hex.
# With a SoftDevice present target_firm is the SD-merged firmware.hex (the
# image you flash over SWD), so uploading that fed a hex to `dfu serial` and
# died in ZipFile with "File is not a zip file". Hand it the DFU package.
upload_source = target_firm
if "nrfutil" == upload_protocol and target_dfu is not None:
    upload_source = target_dfu

env.AddPlatformTarget("upload", upload_source, upload_actions, "Upload")

env.AddPlatformTarget(
    "erase", None, env.VerboseAction("$ERASECMD", "Erasing..."), "Erase Flash")

if any("-Wl,-T" in f for f in env.get("LINKFLAGS", [])):
    print("Warning! '-Wl,-T' option for specifying linker scripts is deprecated. "
          "Please use 'board_build.ldscript' option in your 'platformio.ini' file.")

Default([target_buildprog, target_size])
