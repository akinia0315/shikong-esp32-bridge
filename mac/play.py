#!/usr/bin/env python3
"""Mac 当桥：用 CoreBluetooth 把 shikong 生成的时间表广播出去（吮吸、入体、两路同时都行）。

    pip3 install pyobjc-framework-CoreBluetooth
    python3 mac/play.py suction:60:30:1                     # 吮吸 强度60 30秒 恒定
    python3 mac/play.py suction:60:150:1 insert:58:150:3     # 两路同时，各自到点各自停

通道:强度:秒数[:模式]，模式 1 恒定 / 2 波浪 / 3 脉冲 / 4 攀升。Ctrl-C 随时停（会发全停帧）。

广播部分照着 Moonriseandset/cachito-shikong-mcp 的 terminal_test.py 写（主线程跑 run loop）。
**这个脚本没有在 Mac 实机上跑过**——我们这边没有 Mac，组帧和合排在 ESP32 上验过。
跑之前先退出官方小程序；设备没停的话用设备上的按键停。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shikong import proto, schedule  # noqa: E402


def parse(spec: str) -> tuple[str, int, float, int]:
    parts = spec.split(":")
    if len(parts) not in (3, 4) or parts[0] not in proto.CHANNEL_NAMES:
        raise SystemExit(f"看不懂 {spec!r}，要写成 通道:强度:秒数[:模式]，通道是 suction 或 insert")
    return parts[0], int(parts[1]), float(parts[2]), int(parts[3]) if len(parts) == 4 else 1


def main(argv: list[str]) -> None:
    specs = [parse(s) for s in argv]
    if not specs:
        raise SystemExit(__doc__)

    from CoreBluetooth import CBAdvertisementDataServiceUUIDsKey, CBPeripheralManager, CBUUID
    from Foundation import NSDate, NSObject, NSRunLoop

    class Delegate(NSObject):
        def peripheralManagerDidUpdateState_(self, manager):
            self.ready = manager.state() == 5   # CBManagerStatePoweredOn

        def peripheralManagerDidStartAdvertising_error_(self, manager, error):
            self.error = error
            self.started = True

    delegate = Delegate.alloc().init()
    delegate.ready = False
    delegate.started = False
    delegate.error = None
    manager = CBPeripheralManager.alloc().initWithDelegate_queue_(delegate, None)
    loop = NSRunLoop.currentRunLoop()

    def pump(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))

    deadline = time.monotonic() + 15
    while not delegate.ready and time.monotonic() < deadline:
        pump(0.05)
    if not delegate.ready:
        raise SystemExit("蓝牙没就绪：检查蓝牙开关和终端的蓝牙权限")

    def advertise(frame_hex: str) -> None:
        manager.stopAdvertising()
        delegate.started = False
        delegate.error = None
        manager.startAdvertising_({CBAdvertisementDataServiceUUIDsKey: [CBUUID.UUIDWithString_(proto.cb_uuid(frame_hex))]})
        wait_until = time.monotonic() + 1
        while not delegate.started and time.monotonic() < wait_until:
            pump(0.01)
        if delegate.error:
            print("广播失败：", delegate.error, flush=True)

    start = time.time()
    records = [schedule.channel_record(ch, level, seconds, mode, start) for ch, level, seconds, mode in specs]
    try:
        while True:
            now = time.time()
            records = [r for r in records if r["until"] > now + 0.5]
            if not records:
                break
            steps, _ttl, replan_ms = schedule.compose(records, now)
            window_end = time.monotonic() + (replan_ms / 1000 if replan_ms else sum(s["ms"] for s in steps) / 1000)
            for step in steps:
                left = window_end - time.monotonic()
                if left <= 0:
                    break
                advertise(step["frame"])
                pump(min(step["ms"] / 1000, left))
            if not replan_ms:
                break
    except KeyboardInterrupt:
        print("\n中断，发全停", flush=True)
    finally:
        for step in proto.stop_steps():
            advertise(step["frame"])
            pump(step["ms"] / 1000)
        manager.stopAdvertising()
        print("已发全停帧，请确认设备停了", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
