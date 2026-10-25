"""失控 4.0 的桥：家里的 ESP32 板子长轮询这里，拿到时间表就用 BLE 广播出去。

板子每次 POST /api/toy/bridge/poll
带上自己的状态（以及上一条命令的 ack、扫描结果），服务端把请求挂起最多
``wait`` 秒，一有命令就立刻返回。命令只有一个槽位，新命令顶掉未送达的旧命令。

安全边界：
- 板子有自己的看门狗：超过 watchdog_ms 没联系上服务端就自动发停止帧。
- 每条 play 都带 ttl_ms，到时板子自己停。
- 板上 BOOT 键是物理急停，按下后 30 秒内拒绝新的 play。
- run 默认要求远控窗口已解锁（POST /api/toy/remote 打开），stop 永远不用解锁。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from typing import Any

from . import proto, schedule

log = logging.getLogger("toy_bridge")

# 掉线排查的结论：服务端把长轮询压到 8 秒（见 MAX_POLL_WAIT_SECONDS），板子就从
# 20 秒一次变成 8 秒一次上行——路由器的 station 老化计时一直被刷新，不会把板子当成睡死了踢掉。
# 上行变密之后在线判定才敢收紧：漏三次（约 25 秒）算掉线，面板不会再把掉了一分钟的板子显示成在线。
ONLINE_TTL_SECONDS = 25.0
# 必须大于固件空闲时的轮询间隔：原来 12 秒（当时板子 20 秒一轮），空闲时发指令有一半概率
# 等不到 ack 就报「没及时确认」，调用方看到的是「发了没反应」。
# 长轮询压到 8 秒之后其实用不着这么宽，但板子正好在掉线窗口里时这点余量还有用，先留着。
ACK_TIMEOUT_SECONDS = 28.0
# 固件自己要 20 秒（CONFIG_TOY_POLL_WAIT_SECONDS），服务端按这个上限提前返回。
# 改小是纯服务端的掉线缓解：上行更密 + 掉线能更快被发现 + 新指令最多等 8 秒就被取走。
# keep-alive 还在，所以不会多出 TLS 握手，只多几个空响应。
MAX_POLL_WAIT_SECONDS = 8
# 两次汇报隔这么久就记一笔「断了」。正常 8 秒一次，30 秒＝漏了三次以上。
LINK_GAP_SECONDS = 30.0
# fw 0.3.0 起板子会上报 esp_reset_reason()，不用再靠 uptime 倒退猜是哪种重启。
# 顺序照抄 esp_system.h 的 esp_reset_reason_t，别凭记忆写——9 是掉电、11 是 USB，
# 之前写错位了一格，把刷机后的 USB 复位显示成了 SDIO。`ResetReasonTests` 盯着这张表。
RESET_REASONS = {
    0: "未知", 1: "上电/插拔", 2: "外部复位", 3: "软件重启(esp_restart)", 4: "崩溃(panic)",
    5: "中断看门狗", 6: "任务看门狗", 7: "其它看门狗", 8: "深睡醒来",
    9: "掉电(供电不够)", 10: "SDIO", 11: "USB(刷机或插拔数据线)", 12: "JTAG",
    13: "eFuse 错误", 14: "电源毛刺", 15: "CPU 卡死(double exception)",
}
# 固件上报的最后一次 Wi-Fi 断开 reason code（esp_wifi_types_generic.h 的 wifi_err_reason_t），
# 只留我们真会见到的那几个。同样照抄，别凭记忆——203 是关联失败不是握手。
WIFI_DISC_REASONS = {
    1: "未指明", 2: "认证过期", 3: "对方说要断开", 4: "太久没动静被 AP 踢掉",
    8: "AP 让我们走", 15: "密码握手超时(4-way)",
    200: "没收到 beacon(信号弱或 AP 重启)", 201: "找不到这个 Wi-Fi", 202: "认证失败(密码错?)",
    203: "关联失败", 204: "握手超时", 205: "连接失败",
    206: "AP 时钟重置", 207: "漫游(换到另一个 AP)", 208: "关联等待太久",
}
DEFAULT_WATCHDOG_MS = 15_000
MAX_REMOTE_WINDOW_SECONDS = 12 * 60 * 60
DEFAULT_ADV_INTERVAL_MS = 100
SCAN_DEFAULT_MS = 8000
SCAN_MAX_MS = 30000
# 刚发出去、板子还没把 playing 报上来的宽限期（report() 和 run() 共用）。
PLAY_GRACE_SECONDS = 3.0


class ToyBridgeError(ValueError):
    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _num(value: Any, fallback: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


class ToyBridgeHub:
    def __init__(self) -> None:
        self._device: dict[str, Any] = {}
        self._last_seen = 0.0
        self._remote_expires_at = 0.0
        self._slot: dict[str, Any] | None = None
        self._wake = asyncio.Event()
        self._pending: dict[str, asyncio.Future] = {}
        self._active: dict[str, Any] | None = None       # 最近开的那一路（面板/状态块显示用）
        self._channels: dict[str, dict[str, Any]] = {}   # 每一路各自的 until，两路可以同时跑
        self._last_scan: list[dict[str, Any]] = []
        self._last_scan_at = 0.0
        self._last_frames: list[dict[str, Any]] = []
        self._button_at = 0.0
        self._history: deque[dict[str, Any]] = deque(maxlen=20)
        self._link: deque[dict[str, Any]] = deque(maxlen=40)   # 重启/断连事件，排查掉线用
        self._boots = 0
        self._drops = 0
        self._last_play_at = 0.0      # 最后一次 play 拿到 ack 的时刻（给下面的宽限期用）
        self._stops_seen = -1         # 板子的 stops 计数，涨了就说明它自己停过
        # AI 同一轮连发两条指令时，第二条要等第一条 ack、记进 _channels 之后再排表，
        # 不然排出来的表里没有第一路，到点的全停尾巴会把它一起关掉。stop 不进这把锁。
        self._lock = asyncio.Lock()
        self._gen = 0                 # 每条新命令/停止 +1；接续排表的任务看到号变了就作废
        self._replan_task: asyncio.Task | None = None
        self.adv_interval_ms = DEFAULT_ADV_INTERVAL_MS
        self.adv_connectable = False

    # ------------------------------------------------------------ 板子那一侧

    def report(self, payload: dict[str, Any]) -> None:
        """记录板子的一次汇报；先收扫描结果，再解 ack，这样 scan() 醒来时结果已在。"""
        now = time.time()
        prev_seen, prev_up = self._last_seen, int(_num((self._device or {}).get("uptime_s")))
        self._last_seen = now
        payload = payload if isinstance(payload, dict) else {}
        self._device = {
            "fw": str(payload.get("fw") or "")[:24],
            "chip": str(payload.get("chip") or "")[:16],
            "uptime_s": int(_num(payload.get("uptime_s"))),
            "heap": int(_num(payload.get("heap"))),
            "rssi": int(_num(payload.get("rssi"))),
            "ble_ready": bool(payload.get("ble_ready")),
            "state": str(payload.get("state") or "idle")[:16],
            "cmd_id": str(payload.get("cmd_id") or "")[:32],
            "playing_ms": int(_num(payload.get("playing_ms"))),
            "locked_ms": int(_num(payload.get("locked_ms"))),
            "stops": int(_num(payload.get("stops"))),
            # fw 0.3.0+；老固件没有这几项，取 0
            "reset": int(_num(payload.get("reset"))),
            "wifi_drops": int(_num(payload.get("wifi_drops"))),
            "wifi_reason": int(_num(payload.get("wifi_reason"))),
        }
        self._note_link(now, prev_seen, prev_up)
        scan = payload.get("scan")
        if isinstance(scan, list):
            self._last_scan = [self._clean_scan_entry(item) for item in scan[:40] if isinstance(item, dict)]
            self._last_scan_at = now
            self._last_frames = []
        frames = payload.get("frames")
        if isinstance(frames, list):
            self._last_frames = [
                {
                    "uuid128": str(item.get("uuid128") or "")[:32],
                    "addr": str(item.get("addr") or "")[:17],
                    "n": int(_num(item.get("n"))),
                    "t0": int(_num(item.get("t0"))),
                    "t1": int(_num(item.get("t1"))),
                }
                for item in frames[:64] if isinstance(item, dict)
            ]
            self._last_scan_at = now
        if payload.get("button"):
            self._button_at = now
            self._active = None
            self._channels.clear()
            self._invalidate_plan()
            log.warning("toy bridge: emergency button pressed on device")
        ack = payload.get("ack")
        if isinstance(ack, dict):
            future = self._pending.get(str(ack.get("id") or ""))
            if future is not None and not future.done():
                future.set_result({
                    "ok": bool(ack.get("ok")),
                    "error": str(ack.get("error") or "")[:200],
                })
        # 以前这里要等「计划的结束时间 + 2 秒」才肯清掉记录，于是板子提前停了
        # （掉线超过 15 秒固件看门狗会自己 run_stop，两路一起停）服务端仍然以为它在跑。
        # 下一条命令就会按「叠加到还在跑的那一路上」去排时间表，把一路没人要的又叫起来。
        # 板子每轮都在汇报 state，它说 idle 就是 idle；只留一点宽限期给「刚发出去还没报上来」。
        in_flight = self._slot is not None or bool(self._pending)
        stopped_itself = self._stops_seen >= 0 and self._device["stops"] > self._stops_seen
        self._stops_seen = self._device["stops"]
        if self._device["state"] != "playing" and not in_flight and now - self._last_play_at > PLAY_GRACE_SECONDS:
            if self._channels or self._active:
                planned = max((item.get("until", 0) for item in self._channels.values()), default=0)
                early = max(0.0, planned - now)
                log.warning("toy bridge: 板子报 idle，清掉 %d 路记录（比计划早 %.0f 秒%s）",
                            len(self._channels), early, "，板子自己停的" if stopped_itself else "")
                if early > 2 and stopped_itself:
                    self._link.appendleft({
                        "kind": "selfstop", "at": int(now), "early_s": round(early, 1),
                        "rssi": self._device["rssi"],
                        "why": "板子自己停了（多半是掉线超过 15 秒，固件看门狗）",
                    })
            if self._channels or self._active or self._replan_task:
                self._invalidate_plan()
            self._active = None
            self._channels.clear()
        else:
            self._prune_channels(now)

    def _note_link(self, now: float, prev_seen: float, prev_up: int) -> None:
        """固件不汇报复位原因，只能从 uptime 倒退推断重启、从汇报间隔推断失联。
        「板子一直在重连」这种问题，没有这个记录就只能靠当时有人在看。"""
        up = self._device["uptime_s"]
        if prev_seen and now - prev_seen > LINK_GAP_SECONDS and up >= prev_up:
            self._drops += 1
            self._link.appendleft({
                "kind": "gap", "at": int(now),
                "seconds": round(now - prev_seen, 1),
                "rssi": self._device["rssi"],
                "why": WIFI_DISC_REASONS.get(self._device["wifi_reason"], "") if self._device["wifi_reason"] else "",
            })
            log.warning("toy bridge: 失联 %.0f 秒后回来（第 %d 次），rssi=%s",
                        now - prev_seen, self._drops, self._device["rssi"])
        if prev_seen and up < prev_up:
            self._boots += 1
            self._link.appendleft({
                "kind": "boot", "at": int(now),
                "ran_s": prev_up, "away_s": round(now - prev_seen, 1),
                "rssi": self._device["rssi"], "heap": self._device["heap"],
                "why": RESET_REASONS.get(self._device["reset"], "") if self._device["reset"] else "",
            })
            log.warning("toy bridge: 板子重启了（第 %d 次），上次跑了 %d 秒，离开 %.0f 秒",
                        self._boots, prev_up, now - prev_seen)

    def link_log(self) -> dict[str, Any]:
        return {"boots": self._boots, "drops": self._drops, "events": list(self._link)}

    def _prune_channels(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for key, item in list(self._channels.items()):
            if now > item.get("until", 0):
                self._channels.pop(key, None)

    def active_channels(self) -> list[dict[str, Any]]:
        self._prune_channels()
        if self._device.get("state") != "playing":
            return []
        now = time.time()
        return [
            {**item, "remaining_s": max(0, round(item["until"] - now, 1))}
            for item in sorted(self._channels.values(), key=lambda x: x["started_at"])
        ]

    @staticmethod
    def _clean_scan_entry(item: dict[str, Any]) -> dict[str, Any]:
        out = {
            "addr": str(item.get("addr") or "")[:17],
            "rssi": int(_num(item.get("rssi"))),
            "seen": int(_num(item.get("seen"))),
        }
        for key, limit in (("name", 32), ("uuid128", 32), ("mfg", 64), ("raw", 64)):
            value = item.get(key)
            if value:
                out[key] = str(value)[:limit]
        return out

    async def wait_for_command(self, wait_seconds: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, min(float(wait_seconds), MAX_POLL_WAIT_SECONDS))
        while True:
            if self._slot is not None:
                command = self._slot
                self._slot = None
                self._wake.clear()
                command["delivered_at"] = int(time.time() * 1000)
                return command
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    @property
    def watchdog_ms(self) -> int:
        return DEFAULT_WATCHDOG_MS

    # ------------------------------------------------------------ 状态

    def online(self) -> bool:
        return bool(self._last_seen) and time.time() - self._last_seen <= ONLINE_TTL_SECONDS

    def remote_active(self) -> bool:
        return self.online() and self._remote_expires_at > time.time()

    def set_remote(self, enabled: bool, seconds: float = 3600) -> dict[str, Any]:
        if enabled:
            window = max(60.0, min(float(seconds), MAX_REMOTE_WINDOW_SECONDS))
            self._remote_expires_at = time.time() + window
        else:
            self._remote_expires_at = 0.0
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        active = None
        if self._active and self._device.get("state") == "playing":
            active = {**self._active, "remaining_s": max(0, round(self._active["until"] - now, 1))}
        return {
            "online": self.online(),
            "last_seen_at": int(self._last_seen),
            "seconds_since_seen": round(now - self._last_seen, 1) if self._last_seen else None,
            "device": dict(self._device),
            "channels": self.active_channels(),
            "remote_active": self.remote_active(),
            "remote_expires_at": int(self._remote_expires_at),
            "remote_remaining_s": max(0, int(self._remote_expires_at - now)),
            "active": active,
            "pending_command": bool(self._slot),
            "button_pressed_s_ago": round(now - self._button_at) if self._button_at else None,
            "last_scan_at": int(self._last_scan_at),
            "last_scan_count": len(self._last_scan),
            "link": self.link_log(),
        }

    def last_scan(self) -> dict[str, Any]:
        return {"at": int(self._last_scan_at), "entries": list(self._last_scan), "frames": list(self._last_frames)}

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    # ------------------------------------------------------------ 发命令

    async def _send(
        self,
        command: dict[str, Any],
        *,
        require_remote: bool,
        timeout: float = ACK_TIMEOUT_SECONDS,
        record: bool = True,
    ) -> dict[str, Any]:
        if not self.online():
            raise ToyBridgeError("失控桥不在线：板子没有连上服务器（检查供电和 Wi‑Fi）")
        if command["action"] == "play":
            if require_remote and not self.remote_active():
                raise ToyBridgeError("远控尚未解锁或已经到期")
            locked = int(self._device.get("locked_ms") or 0)
            if locked > 0:
                raise ToyBridgeError(f"板上急停刚按过，{max(1, locked // 1000)} 秒内不接受新的指令")
        command_id = secrets.token_urlsafe(8)
        command["id"] = command_id
        command["issued_at"] = int(time.time() * 1000)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[command_id] = future

        if self._slot is not None:
            old = self._pending.pop(str(self._slot.get("id") or ""), None)
            if old is not None and not old.done():
                old.set_result({"ok": False, "error": "被新命令顶掉"})
        self._slot = command
        self._wake.set()
        try:
            ack = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            if self._slot is command:
                self._slot = None
                self._wake.clear()
            raise ToyBridgeError("板子没有及时确认命令（它可能刚掉线）")
        finally:
            self._pending.pop(command_id, None)
        if record:
            self._history.appendleft({
                "id": command_id,
                "action": command["action"],
                "summary": command.get("summary", ""),
                "ok": bool(ack.get("ok")),
                "error": ack.get("error", ""),
                "at": int(time.time()),
            })
        if not ack.get("ok"):
            raise ToyBridgeError(f"板子拒绝了命令：{ack.get('error') or '未知原因'}")
        return {"command_id": command_id, "ack": ack}

    @staticmethod
    def _validate_run(level: Any, duration_seconds: Any, mode: Any, channel: Any = None) -> tuple[int, float, int, str]:
        clean_channel = str(channel or proto.CHANNEL_SUCTION)
        if clean_channel not in proto.CHANNEL_NAMES:
            raise ToyBridgeError("失控 4.0 只有 suction（吮吸）和 insert（入体）两路", status_code=422)
        try:
            clean_level = int(level)
            clean_mode = int(mode)
            seconds = float(duration_seconds)
        except (TypeError, ValueError):
            raise ToyBridgeError("强度、模式和持续时间必须是数字", status_code=422) from None
        if not 1 <= clean_level <= 100:
            raise ToyBridgeError("强度必须在 1–100", status_code=422)
        if clean_mode not in proto.MODE_NAMES:
            raise ToyBridgeError("模式必须在 1–4", status_code=422)
        if not proto.MIN_DURATION <= seconds <= proto.MAX_DURATION:
            raise ToyBridgeError(f"持续时间必须在 {proto.MIN_DURATION}–{int(proto.MAX_DURATION)} 秒", status_code=422)
        return clean_level, seconds, clean_mode, clean_channel

    async def run(
        self,
        level: Any,
        duration_seconds: Any,
        mode: Any = 1,
        *,
        channel: Any = None,
        require_remote: bool = True,
    ) -> dict[str, Any]:
        clean_level, seconds, clean_mode, clean_channel = self._validate_run(level, duration_seconds, mode, channel)
        channel_name = proto.CHANNEL_NAMES[clean_channel]
        async with self._lock:
            self._invalidate_plan()
            gen = self._gen
            now = time.time()
            self._prune_channels(now)
            # 面板用 active_channels()（会先看板子报的 state），排时间表也必须用同一个判断：
            # 只要板子没在跑，就没有「另一路」可以叠加，哪怕记录里还没到点。
            others = [
                item for key, item in self._channels.items()
                if key != clean_channel and item.get("until", 0) > now + 0.5
            ] if self._board_running(now) else []
            record = {
                "level": clean_level,
                "mode": clean_mode,
                "mode_name": proto.MODE_NAMES[clean_mode],
                "channel": clean_channel,
                "channel_name": channel_name,
                "duration_seconds": seconds,
                "t0": now,
                "started_at": int(now),
                "until": now + seconds,
            }
            replan_ms: int | None = None
            if others:
                steps, ttl_ms, replan_ms = schedule.compose([*others, record], now)
                loop = False
            else:
                steps, loop, ttl_ms = proto.plan(clean_mode, clean_level, seconds, clean_channel)
            summary = f"{channel_name} {proto.MODE_NAMES[clean_mode]} 强度{clean_level} {seconds:g}秒"
            command = {
                "action": "play",
                "steps": steps,
                "loop": loop,
                "ttl_ms": ttl_ms,
                "tail": proto.stop_steps(),   # 两路都停：只停自己那一路会把另一路永远留在跑
                "interval_ms": self.adv_interval_ms,
                "connectable": self.adv_connectable,
                "summary": summary,
            }
            result = await self._send(command, require_remote=require_remote)
            if gen != self._gen:
                # 等 ack 的时候被 stop 抢先了：板子上最后生效的是停止，别再记成在跑。
                return {"ok": True, "summary": summary + "（随后已被停止）", **result, "state": self.snapshot()}
            now = time.time()
            self._last_play_at = now
            # 板子从拿到命令那一刻开始播，计时以 ack 为准（和以前一样）。
            record.update(t0=now, started_at=int(now), until=now + seconds,
                          loop=loop, command_id=result["command_id"])
            self._active = dict(record)
            self._channels[clean_channel] = record
            if replan_ms is not None:
                self._schedule_replan(replan_ms / 1000, gen)
        if others:
            summary += "（和" + "、".join(o.get("channel_name") or "另一路" for o in others) + "同时）"
        return {"ok": True, "summary": summary, **result, "state": self.snapshot()}

    def _board_running(self, now: float) -> bool:
        return self._device.get("state") == "playing" or now - self._last_play_at <= PLAY_GRACE_SECONDS

    def _invalidate_plan(self) -> None:
        self._gen += 1
        task = self._replan_task
        self._replan_task = None
        if task is not None and not task.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if task is not current:
                task.cancel()

    def _schedule_replan(self, delay_s: float, gen: int) -> None:
        self._replan_task = asyncio.get_running_loop().create_task(self._replan_later(max(1.0, delay_s), gen))

    async def _replan_later(self, delay_s: float, gen: int) -> None:
        """上一段快播完了，按两路各自的计时接着排下一段。

        接不上（掉线、被新命令顶掉）就不管：那一段播完板子照常跑全停尾巴，两路一起停，是安全的那一边。
        """
        await asyncio.sleep(delay_s)
        async with self._lock:
            if gen != self._gen:
                return
            now = time.time()
            self._prune_channels(now)
            records = [item for item in self._channels.values() if item.get("until", 0) > now + 0.5]
            if not records or not self._board_running(now):
                return
            steps, ttl_ms, replan_ms = schedule.compose(records, now)
            command = {
                "action": "play",
                "steps": steps,
                "loop": False,
                "ttl_ms": ttl_ms,
                "tail": proto.stop_steps(),
                "interval_ms": self.adv_interval_ms,
                "connectable": self.adv_connectable,
                "summary": "两路接着排",
            }
            try:
                await self._send(command, require_remote=False, record=False)
            except ToyBridgeError as exc:
                log.warning("toy bridge: 两路接续排表没发出去（%s），这段播完板子会全停", exc.detail)
                return
            if gen != self._gen:
                return
            self._last_play_at = time.time()
            if replan_ms is not None:
                self._schedule_replan(replan_ms / 1000, gen)

    async def stop(self) -> dict[str, Any]:
        command = {
            "action": "stop",
            "tail": proto.stop_steps(),
            "interval_ms": self.adv_interval_ms,
            "connectable": self.adv_connectable,
            "summary": "停止",
        }
        self._invalidate_plan()
        result = await self._send(command, require_remote=False)
        self._active = None
        self._channels.clear()
        self._invalidate_plan()
        return {"ok": True, "summary": "已停止", **result, "state": self.snapshot()}

    async def play_raw(self, frames: Any, ms: Any = 2500) -> dict[str, Any]:
        """协议逆向用：直接广播给定的 16 字节帧（人在场时用）。"""
        if not isinstance(frames, (list, tuple)) or not frames:
            raise ToyBridgeError("frames 要是十六进制帧的数组", status_code=422)
        step_ms = int(max(200, min(10000, _num(ms, 2500))))
        steps = []
        for item in list(frames)[:proto.MAX_STEPS]:
            hexed = str(item).strip().lower().replace(" ", "")
            try:
                raw = bytes.fromhex(hexed)
            except ValueError:
                raise ToyBridgeError(f"帧不是十六进制：{item!r}", status_code=422) from None
            if len(raw) != 16:
                raise ToyBridgeError(f"帧必须 16 字节：{hexed}", status_code=422)
            steps.append({"frame": raw.hex(), "ms": step_ms})
        ttl_ms = step_ms * len(steps) + 1000
        command = {
            "action": "play",
            "steps": steps,
            "loop": False,
            "ttl_ms": ttl_ms,
            "tail": proto.stop_steps(),
            "interval_ms": self.adv_interval_ms,
            "connectable": self.adv_connectable,
            "summary": f"原始帧 {len(steps)} 条 × {step_ms}ms",
        }
        self._invalidate_plan()
        result = await self._send(command, require_remote=False)
        self._active = None
        self._channels.clear()
        return {"ok": True, "summary": command["summary"], "steps": steps, **result, "state": self.snapshot()}

    async def scan(self, ms: Any = SCAN_DEFAULT_MS, active: bool = False) -> dict[str, Any]:
        """默认被动扫描：主动扫描会往外发 scan 请求，实测会把手机发给玩具的
        控制帧盖掉（拖滑块玩具不动），抄帧时千万别开。"""
        duration = int(max(500, min(SCAN_MAX_MS, _num(ms, SCAN_DEFAULT_MS))))
        command = {"action": "scan", "ms": duration, "active": bool(active), "summary": f"扫描 {duration} ms"}
        result = await self._send(command, require_remote=False, timeout=duration / 1000 + ACK_TIMEOUT_SECONDS)
        return {"ok": True, **result, "scan": self.last_scan()}

    async def reboot(self) -> dict[str, Any]:
        command = {"action": "reboot", "summary": "重启板子"}
        if not self.online():
            raise ToyBridgeError("失控桥不在线")
        command["id"] = secrets.token_urlsafe(8)
        self._slot = command
        self._wake.set()
        return {"ok": True, "command_id": command["id"]}


toy_hub = ToyBridgeHub()
