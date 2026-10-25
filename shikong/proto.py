"""失控 4.0（蓝牙名 HJ-002）的广播控制帧，吮吸 + 入体两路。

设备不走 GATT：手机把一帧 16 字节编进 128-bit Service UUID 广播出去，设备被动扫描后执行。
这个发现和吮吸那一路的帧格式来自 Moonriseandset/cachito-shikong-mcp；入体那一路、
会话号、两路分开停的掩码是这个仓库用 ESP32 被动扫描官方小程序的广播抄出来的（见 docs/protocol.md）。

帧布局（空中字节序，byte 0 在前；转成 CoreBluetooth 的 UUID 字符串要整体反转，见 cb_uuid()）：

  吮吸运行  [校验] 02 [吮吸档] 00   00     64 00 01 [会话号] 00 51 [序号] 17 00 71
  入体运行  [校验] 02  00      15 [入体档] 2D 00 01 [会话号] 00 52 [序号] 17 00 71
  停        [校验] 00  00      00   00     00 00 01 [会话号] 00 [掩码] [序号] 17 00 71

  校验 = byte1..byte15 求和取低 8 位；序号每帧随机。
  掩码字节：高 4 位 5＝开、0＝停，低位是通道位（1 吮吸、2 入体）；0x03 两路一起停。

纯标准库，不碰蓝牙：ESP32 桥和 Mac 桥都只是把这里生成的帧按时间表广播出去。
"""

from __future__ import annotations

import math
import secrets

MODE_RUN = 0x51              # 吮吸开
MODE_STOP = 0x01             # 只停吮吸
INSERT_MODE_RUN = 0x52       # 入体开
INSERT_MODE_STOP = 0x02      # 只停入体（尾巴里要有它入体才停）
MODE_STOP_BOTH = 0x03        # 两路一起停
# 0x57＝「档位设好但那一路没开」，重放它设备一动不动；0x07＝官方小程序的空闲标记，停不住机器。
# 两个都别拿来当运行帧或停止帧。

# byte8-9 是**会话号**，不是常量。设备只认当前占着它的那一个：
#   0x27C6 是参考仓库里的号，玩具刚上电、没被官方小程序连过时吃；
#   被小程序连过之后只认小程序的号（我们这台是 0x4452）。
# 两路突然一起不动了，先被动扫一次看小程序发的 byte8-9 是什么（tools/decode_frames.py），改这里。
SESSION_ID = (0x44, 0x52)
SESSION_ID_LEGACY = (0x27, 0xC6)

DEVICE_MAX_LEVEL = 100

CHANNEL_SUCTION = "suction"
CHANNEL_INSERT = "insert"
CHANNEL_NAMES = {CHANNEL_SUCTION: "吮吸", CHANNEL_INSERT: "入体"}

# 入体的档位字节是反的：越小越冲（更像周期而不是强度）。用户 1–100 发出去是 100–1。
INSERT_LEVEL_INVERTED = True

MODE_CONSTANT = 1
MODE_WAVE = 2
MODE_PULSE = 3
MODE_CLIMB = 4
MODE_NAMES = {MODE_CONSTANT: "恒定", MODE_WAVE: "波浪", MODE_PULSE: "脉冲", MODE_CLIMB: "攀升"}

MAX_STEPS = 100              # 固件 PLAYER_MAX_STEPS
MAX_TAIL = 4                 # 固件 PLAYER_MAX_TAIL：多出来的尾巴帧会被**静默丢掉**
MIN_DURATION = 0.5
MAX_DURATION = 300.0


def user_to_device(level: int, channel: str = CHANNEL_SUCTION) -> int:
    """用户 1–100 → 设备档位；0 表示停。

    吮吸用原仓库按官方小程序抓包拟合的 50 + (pct+1)//2；入体反着发 101 - pct。
    """
    level = max(0, min(100, int(level)))
    if level <= 0:
        return 0
    if channel == CHANNEL_INSERT:
        return 101 - level if INSERT_LEVEL_INVERTED else level
    return min(DEVICE_MAX_LEVEL, 50 + (level + 1) // 2)


def build_frame(
    device_level: int,
    mode: int,
    sequence: int | None = None,
    channel: str = CHANNEL_SUCTION,
    session: tuple[int, int] = SESSION_ID,
) -> bytes:
    """一帧 16 字节（空中字节序）。mode 是掩码字节；停止帧的前半段全写 0（照抄官方）。"""
    device_level = max(0, min(DEVICE_MAX_LEVEL, int(device_level)))
    if sequence is None:
        sequence = secrets.randbelow(256)
    mode &= 0xFF
    running = mode >> 4 == 0x5
    if channel == CHANNEL_INSERT:
        body = [0x02, 0x00, 0x15, device_level, 0x2D] if running else [0x00] * 5
    else:
        # 吮吸的停止帧也用这个布局、档位 0（原仓库的写法，实机验过能停）
        body = [0x02, device_level if running else 0x00, 0x00, 0x00, 0x64]
    frame = bytearray([0x00, *body, 0x00, 0x01, *session, 0x00, mode, sequence & 0xFF, 0x17, 0x00, 0x71])
    frame[0] = sum(frame[1:]) & 0xFF
    return bytes(frame)


def frame_hex(device_level: int, mode: int, sequence: int | None = None, channel: str = CHANNEL_SUCTION) -> str:
    return build_frame(device_level, mode, sequence, channel).hex()


def cb_uuid(frame: bytes | str) -> str:
    """空中字节序的帧 → CoreBluetooth 的 UUID 字符串（Mac 桥用；整体反转 16 字节）。"""
    raw = bytes.fromhex(frame) if isinstance(frame, str) else bytes(frame)
    r = raw[::-1].hex().upper()
    return f"{r[0:8]}-{r[8:12]}-{r[12:16]}-{r[16:20]}-{r[20:32]}"


def run_step(user_level: int, ms: int, channel: str = CHANNEL_SUCTION) -> dict:
    mode = INSERT_MODE_RUN if channel == CHANNEL_INSERT else MODE_RUN
    return {"frame": frame_hex(user_to_device(user_level, channel), mode, channel=channel), "ms": int(ms)}


def stop_steps(channel: str | None = None, solo: bool = False) -> list[dict]:
    """停止帧；总数不超过 MAX_TAIL。

    默认三种掩码都发（0x01 吮吸、0x02 入体、0x03 两路），谁被落下都停。
    solo=True 只发这一路自己的掩码——两路同时跑、只想停其中一路时必须这样，
    不然 0x03 会把另一路也掐掉。
    """
    if channel == CHANNEL_SUCTION:
        pair = (CHANNEL_SUCTION, MODE_STOP)
        plan_ = [pair] * 2 if solo else [pair, (CHANNEL_INSERT, MODE_STOP_BOTH)] * 2
    elif channel == CHANNEL_INSERT:
        pair = (CHANNEL_INSERT, INSERT_MODE_STOP)
        plan_ = [pair] * 2 if solo else [pair, (CHANNEL_INSERT, MODE_STOP_BOTH)] * 2
    else:
        plan_ = [
            (CHANNEL_SUCTION, MODE_STOP),
            (CHANNEL_INSERT, INSERT_MODE_STOP),
            (CHANNEL_INSERT, MODE_STOP_BOTH),
            (CHANNEL_INSERT, INSERT_MODE_STOP),
        ]
    return [{"frame": frame_hex(0, mode, channel=ch), "ms": 250} for ch, mode in plan_[:MAX_TAIL]]


def pattern(mode: int, level: int, duration_seconds: float) -> tuple[list[tuple[int, int]], bool, int]:
    """一条指令的节奏：([(用户档位, ms), ...], 是否循环, 总时长 ms)。"""
    level = max(1, min(100, int(level)))
    seconds = max(MIN_DURATION, min(MAX_DURATION, float(duration_seconds)))
    total_ms = int(seconds * 1000)
    mode = int(mode)

    if mode == MODE_WAVE:
        # 10 秒一个周期，在 level 的一半到 level 之间正弦起伏
        low = max(1, level // 2)
        seq = []
        for i in range(20):
            phase = (math.sin(i * 2 * math.pi / 20 - math.pi / 2) + 1) / 2
            seq.append((int(round(low + (level - low) * phase)), 500))
        return seq, True, total_ms

    if mode == MODE_PULSE:
        # 3 秒强、3 秒最轻（不完全停，避免每次重新启动的迟滞）
        return [(level, 3000), (1, 3000)], True, total_ms

    if mode == MODE_CLIMB:
        count = max(2, min(MAX_STEPS, int(seconds * 2)))
        step_ms = max(20, int(total_ms / count))
        return [(int(round(1 + (level - 1) * i / (count - 1))), step_ms) for i in range(count)], False, total_ms

    return [(level, total_ms)], False, total_ms


def level_events(mode: int, level: int, duration_seconds: float) -> list[tuple[int, int]]:
    """把节奏摊开成 [(从开始算的 ms, 用户档位), ...]，循环的展开到总时长；档位没变的点不列。"""
    seq, loop, total_ms = pattern(mode, level, duration_seconds)
    events: list[tuple[int, int]] = []
    t = 0
    while t < total_ms:
        for lvl, ms in seq:
            if t >= total_ms:
                break
            if not events or events[-1][1] != lvl:
                events.append((t, lvl))
            t += ms
        if not loop:
            break
    return events


def plan(
    mode: int,
    level: int,
    duration_seconds: float,
    channel: str = CHANNEL_SUCTION,
) -> tuple[list[dict], bool, int]:
    """单路指令 → 固件时间表 (steps, loop, ttl_ms)。"""
    seq, loop, total_ms = pattern(mode, level, duration_seconds)
    steps = [run_step(lvl, ms, channel) for lvl, ms in seq]
    return steps, loop, total_ms if loop else total_ms + 1000
