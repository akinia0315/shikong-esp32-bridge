"""命令行：把指令展开成时间表 JSON，给 Mac 桥（mac/play.py）或别的广播器用。

    python -m shikong plan suction:60:30:1                 # 吮吸 强度60 30秒 恒定
    python -m shikong plan suction:60:150:1 insert:58:150:3  # 两路同时
    python -m shikong frame insert 40 --uuid               # 单帧，附 CoreBluetooth UUID 字符串

通道:强度:秒数:模式，模式 1 恒定 / 2 波浪 / 3 脉冲 / 4 攀升。
"""

from __future__ import annotations

import argparse
import json
import time

from . import proto, schedule


def _parse(spec: str) -> tuple[str, int, float, int]:
    parts = spec.split(":")
    if len(parts) not in (3, 4) or parts[0] not in proto.CHANNEL_NAMES:
        raise SystemExit(f"看不懂 {spec!r}，要写成 通道:强度:秒数[:模式]，通道是 suction 或 insert")
    return parts[0], int(parts[1]), float(parts[2]), int(parts[3]) if len(parts) == 4 else 1


def _with_uuid(steps: list[dict]) -> list[dict]:
    return [{**s, "uuid": proto.cb_uuid(s["frame"])} for s in steps]


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m shikong")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="一路或两路指令 → 时间表")
    p_plan.add_argument("specs", nargs="+", help="通道:强度:秒数[:模式]")
    p_frame = sub.add_parser("frame", help="单帧")
    p_frame.add_argument("channel", choices=sorted(proto.CHANNEL_NAMES))
    p_frame.add_argument("level", type=int, help="1–100；0 表示这一路的停止帧")
    p_frame.add_argument("--uuid", action="store_true", help="附 CoreBluetooth UUID 字符串")
    args = parser.parse_args()

    if args.cmd == "frame":
        if args.level > 0:
            out = proto.run_step(args.level, 0, args.channel)
        else:
            out = proto.stop_steps(args.channel, solo=True)[0]
        out = {"frame": out["frame"]}
        if args.uuid:
            out["uuid"] = proto.cb_uuid(out["frame"])
        print(json.dumps(out, ensure_ascii=False))
        return

    specs = [_parse(s) for s in args.specs]
    if len({s[0] for s in specs}) != len(specs):
        raise SystemExit("同一路只能写一次")
    if len(specs) == 1:
        ch, level, seconds, mode = specs[0]
        steps, loop, ttl_ms = proto.plan(mode, level, seconds, ch)
        replan_ms = None
    else:
        now = time.time()
        records = [schedule.channel_record(ch, level, seconds, mode, now) for ch, level, seconds, mode in specs]
        steps, ttl_ms, replan_ms = schedule.compose(records, now)
        loop = False
    print(json.dumps({
        "steps": _with_uuid(steps),
        "loop": loop,
        "ttl_ms": ttl_ms,
        "tail": _with_uuid(proto.stop_steps()),
        "replan_after_ms": replan_ms,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
