Import("env")
import os

def merge_bin(source, target, env):
    firmware = str(target[0])
    board = env.BoardConfig()
    mcu = board.get("build.mcu", "esp32")
    flash_mode = env.GetProjectOption("board_build.flash_mode", "dio")
    mem_type = env.GetProjectOption("board_build.arduino.memory_type", "")
    # esptool merge_bin 不支持 opi；octal flash(OPI) 板启动头模式与平台逻辑一致用 dout
    # （platform-espressif32 builder/main.py _get_board_flash_mode：opi_opi/opi_qspi → dout）
    if flash_mode == "opi" or mem_type in ("opi_opi", "opi_qspi"):
        flash_mode = "dout"
    flash_freq = board.get("build.f_flash", "40000000L").replace("L", "")
    flash_freq_m = str(int(int(flash_freq) / 1000000)) + "m"
    flash_size = board.get("upload.flash_size", "4MB")

    esptool = os.path.join(
        env.PioPlatform().get_package_dir("tool-esptoolpy") or "",
        "esptool.py",
    )
    output = os.path.join(env.subst("$BUILD_DIR"), "firmware_merged.bin")

    extra = env.get("FLASH_EXTRA_IMAGES", [])
    print("=== FLASH_EXTRA_IMAGES ===")
    for offset, path in extra:
        print(f"  {offset} -> {env.subst(path)}")
    print(f"  APP -> 0x10000 {firmware}")
    print("==========================")

    cmd = [
        "$PYTHONEXE", esptool,
        "--chip", mcu,
        "merge_bin",
        "--flash_mode", flash_mode,
        "--flash_freq", flash_freq_m,
        "--flash_size", flash_size,
        "-o", output,
    ]

    for offset, path in extra:
        cmd.extend([offset, env.subst(path)])

    cmd.extend(["0x10000", firmware])

    env.Execute(env.VerboseAction(" ".join(cmd), "Merging firmware into single binary"))

env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", merge_bin)
