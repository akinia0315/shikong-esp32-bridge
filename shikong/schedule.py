"""两路同时跑的时间表。纯函数，ESP32 桥和 Mac 桥共用。

设备两路各自记状态：一路换档只要广播那一路的帧，另一路照旧在跑。所以「同时」不是两路
一直交替刷，而是把两路各自的换档时刻摊开、按时间合进一张表，每次换档只播那一路的帧
（至少 MIN_FRAME_MS，设备才接得住）。

- 谁先到点，就在那一刻插它**自己**的停止帧（solo 掩码；0x03 会把另一路也掐掉），
  再给还在跑的那一路补一帧。
- 最后一路到点时表就播完了，桥照常跑全停尾巴。
- 节奏按每一路自己的开始时刻算：后叠上去的那一路不会把先在跑的那一路拉回开头。
- 波浪半秒一换，一张表（MAX_STEPS 步）装不下全程，就只排前面一段，返回「多少 ms 后
  接着排」；没接上的话这段播完全停，是安全的那一边。
"""

from __future__ import annotations

from typing import Any

from . import proto

MIN_FRAME_MS = 250
STEP_BUDGET = proto.MAX_STEPS - 6     # 留几步给停止帧和补帧
REPLAN_LEAD_MS = 3000                 # 一段快播完前这么久接着排下一段
WINDOW_SLACK_MS = 4000                # 这段的最后一帧多播一会儿，等下一段接上


def channel_record(channel: str, level: int, seconds: float, mode: int, t0: float) -> dict[str, Any]:
    """compose() 认的一路记录。t0 是这一路开始的时刻（time.time()）。"""
    return {
        "channel": channel,
        "level": int(level),
        "mode": int(mode),
        "duration_seconds": float(seconds),
        "t0": float(t0),
        "until": float(t0) + float(seconds),
    }


def compose(records: list[dict[str, Any]], now: float) -> tuple[list[dict], int, int | None]:
    """records 里每一路（见 channel_record）→ (steps, ttl_ms, 多少 ms 后接着排 或 None)。

    steps 是非循环的 [{"frame": hex, "ms": int}, ...]，从 now 开始播。
    """
    events: list[tuple[int, int, str, int]] = []   # (离现在 ms, 顺序, 通道, 档位；0＝这一路到点)
    for order, rec in enumerate(records):
        ch = rec["channel"]
        elapsed = int((now - rec["t0"]) * 1000)
        end_ms = max(0, int((rec["until"] - now) * 1000))
        current = None
        for off, lvl in proto.level_events(rec["mode"], rec["level"], rec["duration_seconds"]):
            if off <= elapsed:
                current = lvl
            elif off - elapsed < end_ms:
                events.append((off - elapsed, order, ch, lvl))
        events.append((0, order, ch, current or rec["level"]))
        events.append((end_ms, order, ch, 0))
    events.sort()

    timeline: list[tuple[int, str]] = []   # (开始 ms, 帧)
    levels: dict[str, int] = {}
    active = {rec["channel"] for rec in records}
    cursor = 0
    end_ms = 0
    cut: int | None = None

    def emit(start: int, frame: str, ms: int = MIN_FRAME_MS) -> int:
        timeline.append((start, frame))
        return start + ms

    for t, _, ch, lvl in events:
        if ch not in active:
            continue
        start = max(t, cursor)
        if len(timeline) >= STEP_BUDGET:
            cut = start
            break
        if lvl == 0:
            active.discard(ch)
            if not active:
                end_ms = start
                break
            for stop in proto.stop_steps(ch, solo=True):
                start = emit(start, stop["frame"], stop["ms"])
            for other in active:
                start = emit(start, proto.run_step(levels[other], 0, other)["frame"])
            cursor = start
            continue
        if levels.get(ch) == lvl:
            continue
        levels[ch] = lvl
        cursor = emit(start, proto.run_step(lvl, 0, ch)["frame"])

    window_end = cut + WINDOW_SLACK_MS if cut is not None else end_ms
    steps = []
    for i, (start, frame) in enumerate(timeline):
        nxt = timeline[i + 1][0] if i + 1 < len(timeline) else window_end
        steps.append({"frame": frame, "ms": max(MIN_FRAME_MS, nxt - start)})
    ttl_ms = sum(step["ms"] for step in steps) + 1000
    replan_ms = max(1000, cut - REPLAN_LEAD_MS) if cut is not None else None
    return steps, ttl_ms, replan_ms
