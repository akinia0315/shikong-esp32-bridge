#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""失控桥 · 一键刷机

把这个文件和 shikong-bridge-*.bin 放在同一个文件夹里，然后运行它：
  Windows：双击 flash.py，或在文件夹里打开命令行输入  python flash.py
  Mac：终端里  python3 flash.py
它会自动找板子、认芯片和晶振、挑对的固件、刷进去。全程跟着提示按回车就行。
"""
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_FOR = {
    ("ESP32-C2", 40): "shikong-bridge-esp32c2-40mhz.bin",
    ("ESP32-C2", 26): "shikong-bridge-esp32c2-26mhz.bin",
    ("ESP32-C3", None): "shikong-bridge-esp32c3.bin",
    ("ESP32", None): "shikong-bridge-esp32.bin",
}
# 升级用：只刷 app 分区（0x10000），Wi-Fi 配置和令牌都在 nvs 里，不会被动到。
APP_BIN_FOR = {
    ("ESP32-C2", 40): "shikong-bridge-esp32c2-40mhz-app-only.bin",
    ("ESP32-C2", 26): "shikong-bridge-esp32c2-26mhz-app-only.bin",
    ("ESP32-C3", None): "shikong-bridge-esp32c3-app-only.bin",
    ("ESP32", None): "shikong-bridge-esp32-app-only.bin",
}
APP_OFFSET = "0x10000"
KNOWN_USB = {
    (0x1A86, 0x7523): "CH340", (0x1A86, 0x55D4): "CH9102", (0x1A86, 0x55D3): "CH343",
    (0x10C4, 0xEA60): "CP210x", (0x0403, 0x6001): "FTDI", (0x0403, 0x6015): "FTDI",
    (0x303A, 0x1001): "ESP32 USB", (0x303A, 0x0002): "ESP32 USB",
}


def say(msg=""):
    print(msg, flush=True)


def ask(msg="按回车继续…"):
    try:
        return input(msg).strip()
    except EOFError:
        return ""


def finish(code=0):
    say()
    ask("按回车关闭窗口。")
    sys.exit(code)


def ensure_esptool():
    try:
        import esptool  # noqa: F401
        import serial.tools.list_ports  # noqa: F401
        return
    except ImportError:
        pass
    say("第一次用，先装刷机工具 esptool（需要联网，一两分钟）…")
    cmds = [[sys.executable, "-m", "pip", "install", "esptool"],
            [sys.executable, "-m", "pip", "install", "--user", "esptool"]]
    for cmd in cmds:
        try:
            subprocess.check_call(cmd)
            break
        except subprocess.CalledProcessError:
            continue
    else:
        say("装不上 esptool。请手动在命令行运行：  python -m pip install esptool   然后再运行我。")
        finish(1)
    say("装好了，重新启动脚本…")
    code = subprocess.call([sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    sys.exit(code)


def list_ports():
    from serial.tools import list_ports
    out = []
    for p in list_ports.comports():
        if sys.platform == "darwin" and "/dev/tty." in p.device:
            continue  # Mac 上 cu.* 和 tty.* 是同一个口，只留 cu
        if sys.platform.startswith("linux") and p.device.startswith("/dev/ttyS"):
            continue  # Linux 的 ttyS* 是主板串口，板子会是 ttyUSB* / ttyACM*
        out.append(p)
    return out


def describe(p):
    kind = KNOWN_USB.get((p.vid, p.pid), "")
    return f"{p.device}  {kind or (p.description or '')}".strip()


def find_port():
    say("先找板子插在哪个串口。")
    say("  第 1 步：如果板子现在插着，把它拔下来。")
    ask("  拔好了按回车…")
    before = {p.device for p in list_ports()}
    say("  第 2 步：把板子用【数据线】插到电脑上，等 3 秒。")
    ask("  插好了按回车…")
    time.sleep(1.5)
    after = list_ports()
    new = [p for p in after if p.device not in before]
    if len(new) == 1:
        say(f"找到了：{describe(new[0])}")
        return new[0].device
    likely = [p for p in after if (p.vid, p.pid) in KNOWN_USB]
    if len(new) > 1:
        candidates = new
    elif len(likely) == 1:
        say(f"插拔没看到新串口，但有一个像是板子的口：{describe(likely[0])}")
        return likely[0].device
    elif likely:
        candidates = likely
    elif after:
        candidates = after
    else:
        say()
        say("电脑上一个串口都没有。最常见的原因：")
        say("  1) 用的是只能充电的线，换一根能传数据的 USB 线；")
        say("  2) 电脑缺驱动。板子上的 USB 转串口芯片多半是 CH340 或 CP2102：")
        say("     Windows 装 CH341SER 驱动（搜「CH340 驱动」），Mac 一般自带。")
        say("  装好后重新运行我。")
        finish(1)
    say("有好几个串口，哪个是板子？（刚插上板子才出现的那个）")
    for i, p in enumerate(candidates, 1):
        say(f"  {i}) {describe(p)}")
    while True:
        choice = ask("输入序号后按回车：")
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1].device


def detect(port):
    from esptool.cmds import detect_chip
    for attempt in range(1, 4):
        try:
            esp = detect_chip(port, 115200, "default_reset", False, 3)
        except Exception as exc:  # noqa: BLE001
            say(f"连不上板子（第 {attempt} 次）：{str(exc)[:160]}")
            if attempt == 3:
                say()
                say("试试手动进下载模式：按住板上的 BOOT 键不放，按一下 RST 键（没有 RST 就拔掉重插线），")
                say("然后松开 BOOT，再运行我一次。还不行就换根线、换个 USB 口。")
                finish(1)
            say("按住板上 BOOT 键不放，按一下 RST（或者拔了重插），松开，再试…")
            ask()
            continue
        name = esp.CHIP_NAME
        xtal = None
        if "C2" in name:
            try:
                xtal = int(esp.get_crystal_freq())
            except Exception:  # noqa: BLE001
                xtal = 40
        try:
            desc = esp.get_chip_description()
        except Exception:  # noqa: BLE001
            desc = name
        try:
            esp._port.close()
        except Exception:  # noqa: BLE001
            pass
        return name, xtal, desc
    return None, None, None


def pick_bin(name, xtal, app_only=False):
    key = (name, xtal if "C2" in name else None)
    filename = (APP_BIN_FOR if app_only else BIN_FOR).get(key)
    if not filename:
        say(f"这块板子是 {name}，我这里没有它的固件。去 GitHub 提个 issue，带上这行：{name} xtal={xtal}")
        finish(1)
    path = os.path.join(HERE, filename)
    if not os.path.exists(path):
        say(f"需要固件文件 {filename}，但它不在这个文件夹里。")
        say("去 GitHub Releases 下载它，放到和 flash.py 同一个文件夹，再运行我。")
        finish(1)
    return path


def flash(port, chip_arg, path, offset="0x0"):
    import esptool
    # esptool v4 用下划线（write_flash / default_reset），v5 改成了连字符。
    # 两种拼法都试一遍，省得撞上版本不一样就刷不了。
    spellings = [("write_flash", "default_reset", "hard_reset"),
                 ("write-flash", "default-reset", "hard-reset")]
    for baud in ("460800", "115200"):
        for write, before, after in spellings:
            args = ["--chip", chip_arg, "--port", port, "--baud", baud,
                    "--before", before, "--after", after,
                    write, offset, path]
            say(f"开始刷写（{baud} 波特，{write} {offset}）…")
            try:
                esptool.main(args)
                return True
            except SystemExit as exc:
                if exc.code in (0, None):
                    return True
                say(f"刷写失败（退出码 {exc.code}），换个写法/速度再试。")
            except Exception as exc:  # noqa: BLE001
                say(f"刷写失败：{str(exc)[:160]}，换个写法/速度再试。")
    return False


def choose_mode(name, xtal):
    """问是「升级（保配置）」还是「整块重刷（清配置）」。

    已经配过网的板子只刷 app 分区就够了，Wi-Fi 名字密码和令牌都在 nvs 里不会被动到；
    整块刷会把 nvs 一起覆盖，刷完要拿手机重新配一次网。
    """
    key = (name, xtal if "C2" in name else None)
    app_name = APP_BIN_FOR.get(key)
    if not app_name or not os.path.exists(os.path.join(HERE, app_name)):
        return False
    say()
    say("这块板子以前配过网吗？")
    say("  1) 配过，只升级固件 —— Wi‑Fi 配置和令牌都留着，刷完自己连回来（推荐）")
    say("  2) 没配过 / 想从头来 —— 整块重刷，刷完要用手机重新配网")
    while True:
        choice = ask("输入 1 或 2 后按回车（直接回车＝1）：")
        if choice in ("", "1"):
            return True
        if choice == "2":
            return False


def main():
    say("=== 失控桥 · 一键刷机 ===")
    bins = sorted(os.path.basename(p) for p in glob.glob(os.path.join(HERE, "shikong-bridge-*.bin")))
    say("文件夹里的固件：" + (", ".join(bins) if bins else "一个都没有！先去 GitHub Releases 下载。"))
    if not bins:
        finish(1)
    ensure_esptool()
    if len(sys.argv) > 1 and sys.argv[1].strip():
        port = sys.argv[1].strip()          # 例如  python flash.py COM5
        say(f"用你指定的串口：{port}")
    else:
        port = find_port()
    say()
    say("正在识别芯片…")
    name, xtal, desc = detect(port)
    say(f"芯片：{desc}" + (f"，晶振 {xtal} MHz" if xtal else ""))
    app_only = choose_mode(name, xtal)
    path = pick_bin(name, xtal, app_only)
    say(f"用这个固件：{os.path.basename(path)}")
    chip_arg = name.lower().replace("-", "")
    ok = flash(port, chip_arg, path, APP_OFFSET if app_only else "0x0")
    say()
    if not ok:
        say("两种速度都没刷成。把上面的报错截图，去 GitHub 提个 issue。")
        finish(1)
    say("✅ 刷好了！板子已经自动重启。")
    say()
    if app_only:
        say("配置没动，它会自己连回家里的 Wi‑Fi。")
        say("把板子插回电源，去服务端 GET /api/toy/state 看 online 变成 true 就行。")
    else:
        say("接下来配网：")
        say("  1) 手机 Wi‑Fi 里找一个叫 Shikong-Bridge 的网络，连上（没密码）。")
        say("  2) 浏览器打开  http://192.168.4.1/")
        say("  3) 填家里的 2.4G Wi‑Fi 名字和密码、服务器地址、令牌（服务端的 SHIKONG_BRIDGE_TOKEN）。保存。")
        say("  4) 去服务端 GET /api/toy/state 看 online 变成 true。")
    finish(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        say(f"出错了：{exc.__class__.__name__}: {str(exc)[:300]}")
        finish(1)
