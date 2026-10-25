import asyncio
import time
import unittest

from shikong import proto
from shikong.hub import ToyBridgeError, ToyBridgeHub


class ShikongProtoTests(unittest.TestCase):
    def test_suction_frame_layout(self):
        frame = proto.build_frame(75, proto.MODE_RUN, sequence=0x10)
        self.assertEqual(len(frame), 16)
        self.assertEqual(frame[1:12], bytes([0x02, 75, 0x00, 0x00, 0x64, 0x00, 0x01, *proto.SESSION_ID, 0x00, 0x51]))
        self.assertEqual(frame[12], 0x10)
        self.assertEqual(frame[13:], bytes([0x17, 0x00, 0x71]))
        self.assertEqual(frame[0], sum(frame[1:]) & 0xFF)

    def test_legacy_session_rebuilds_reference_repo_frame(self):
        """原仓库 cachito_mcp.py 的 _build_uuid(75, 0x51) 在序号 0x10 时的结果。"""
        ref = [0x00, 0x02, 75, 0x00, 0x00, 0x64, 0x00, 0x01, 0x27, 0xC6, 0x00, 0x51, 0x10, 0x17, 0x00, 0x71]
        ref[0] = sum(ref[1:]) & 0xFF
        got = proto.build_frame(75, proto.MODE_RUN, sequence=0x10, session=proto.SESSION_ID_LEGACY)
        self.assertEqual(got, bytes(ref))

    def test_cb_uuid_reverses_the_air_order(self):
        frame = bytes(range(16))
        self.assertEqual(proto.cb_uuid(frame), "0F0E0D0C-0B0A-0908-0706-050403020100")

    def test_suction_levels_match_official_captures(self):
        # 原仓库 10 月抓的官方档位：5/10/30/50 → 53/55/65/75
        for pct, dev in ((5, 53), (10, 55), (30, 65), (50, 75)):
            self.assertEqual(proto.user_to_device(pct), dev)
        self.assertEqual(proto.user_to_device(100), 100)
        self.assertEqual(proto.user_to_device(0), 0)

    def test_insert_frames_match_captured_broadcasts(self):
        """「入体真的在动」时用 ESP32 被动扫描抄到的官方小程序帧（samples/insert-running.json）。"""
        for want in (
            "2d0200153e2d0001445200523a170071",
            "12020015022d0001445200525b170071",
            "01020015472d00014452005205170071",
            "c4020015072d00014452005208170071",
        ):
            raw = bytes.fromhex(want)
            self.assertEqual(
                proto.build_frame(raw[4], proto.INSERT_MODE_RUN, sequence=raw[12], channel=proto.CHANNEL_INSERT).hex(),
                want,
            )

    def test_every_captured_frame_satisfies_the_checksum(self):
        import json
        from pathlib import Path

        data = json.loads((Path(__file__).resolve().parent.parent / "samples" / "insert-running.json").read_text())
        frames = [bytes.fromhex(f["uuid128"]) for f in data["frames"]]
        self.assertTrue(frames)
        for frame in frames:
            self.assertEqual(frame[0], sum(frame[1:]) & 0xFF, frame.hex())

    def test_stop_frames(self):
        suction_stop = proto.build_frame(0, proto.MODE_STOP, sequence=0)
        self.assertEqual(suction_stop[1:6], bytes([0x02, 0x00, 0x00, 0x00, 0x64]))
        self.assertEqual(suction_stop[11], 0x01)
        for mode in (proto.INSERT_MODE_STOP, proto.MODE_STOP_BOTH):
            frame = proto.build_frame(0, mode, sequence=0, channel=proto.CHANNEL_INSERT)
            self.assertEqual(frame[1:6], bytes(5), "停止帧前半段要全 0（照抄官方）")
            self.assertEqual(frame[11], mode)
        self.assertNotEqual(proto.INSERT_MODE_STOP, 0x07, "0x07 是小程序的空闲标记，停不住")

    def test_stop_steps_without_channel_cover_both(self):
        modes = [bytes.fromhex(step["frame"])[11] for step in proto.stop_steps()]
        self.assertEqual(
            modes,
            [proto.MODE_STOP, proto.INSERT_MODE_STOP, proto.MODE_STOP_BOTH, proto.INSERT_MODE_STOP],
        )

    def test_solo_stop_never_touches_the_other_channel(self):
        self.assertEqual({bytes.fromhex(s["frame"])[11] for s in proto.stop_steps(proto.CHANNEL_SUCTION, solo=True)},
                         {proto.MODE_STOP})
        self.assertEqual({bytes.fromhex(s["frame"])[11] for s in proto.stop_steps(proto.CHANNEL_INSERT, solo=True)},
                         {proto.INSERT_MODE_STOP})

    def test_tails_never_exceed_firmware_cap(self):
        """固件 PLAYER_MAX_TAIL=4，超出的尾巴帧会被静默丢掉（入体停不下来就是这么来的）。"""
        self.assertEqual(proto.MAX_TAIL, 4)
        for ch in (None, proto.CHANNEL_SUCTION, proto.CHANNEL_INSERT):
            self.assertLessEqual(len(proto.stop_steps(ch)), proto.MAX_TAIL)

    def test_insert_level_is_inverted(self):
        self.assertEqual(proto.user_to_device(1, proto.CHANNEL_INSERT), 100)
        self.assertEqual(proto.user_to_device(100, proto.CHANNEL_INSERT), 1)
        self.assertEqual(proto.user_to_device(0, proto.CHANNEL_INSERT), 0)

    def test_insert_plans_stay_on_insert_channel(self):
        for mode in (1, 2, 3, 4):
            steps, _loop, _ttl = proto.plan(mode, 80, 60, proto.CHANNEL_INSERT)
            self.assertLessEqual(len(steps), proto.MAX_STEPS)
            for step in steps:
                frame = bytes.fromhex(step["frame"])
                self.assertEqual(frame[11], proto.INSERT_MODE_RUN)
                self.assertEqual(frame[3], 0x15)
                self.assertEqual(frame[5], 0x2D)

    def test_plans_stay_within_firmware_limits(self):
        for mode in (1, 2, 3, 4):
            steps, loop, ttl = proto.plan(mode, 80, 300)
            self.assertLessEqual(len(steps), proto.MAX_STEPS)
            self.assertGreaterEqual(ttl, 300_000)
            for step in steps:
                self.assertEqual(len(bytes.fromhex(step["frame"])), 16)
                self.assertGreaterEqual(step["ms"], 20)
        steps, loop, _ = proto.plan(2, 80, 30)
        self.assertTrue(loop)
        self.assertEqual(len(steps), 20)
        steps, loop, _ = proto.plan(4, 60, 5)
        self.assertFalse(loop)
        self.assertEqual(bytes.fromhex(steps[-1]["frame"])[2], proto.user_to_device(60))


class ToyBridgeHubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hub = ToyBridgeHub()

    def report(self, **extra):
        payload = {"fw": "0.1.0", "chip": "esp32c2", "state": "idle", "ble_ready": True}
        payload.update(extra)
        self.hub.report(payload)

    async def board_turn(self, wait=1.0, **extra):
        """One firmware poll: report, then wait for a command."""
        self.report(**extra)
        return await self.hub.wait_for_command(wait)

    async def test_run_round_trip(self):
        self.report()
        self.hub.set_remote(True, 600)
        task = asyncio.create_task(self.hub.run(70, 5, 2))
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.assertEqual(command["action"], "play")
        self.assertTrue(command["loop"])
        self.assertEqual(len(command["tail"]), proto.MAX_TAIL)
        self.assertEqual(
            [bytes.fromhex(t["frame"])[11] for t in command["tail"]],
            [proto.MODE_STOP, proto.INSERT_MODE_STOP, proto.MODE_STOP_BOTH, proto.INSERT_MODE_STOP],
        )
        self.assertEqual(command["ttl_ms"], 5000)
        self.report(state="playing", cmd_id=command["id"], ack={"id": command["id"], "ok": True})
        result = await asyncio.wait_for(task, timeout=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["active"]["level"], 70)
        self.assertEqual(result["state"]["active"]["mode_name"], "波浪")

    async def test_run_requires_remote_but_stop_does_not(self):
        self.report()
        with self.assertRaises(ToyBridgeError):
            await self.hub.run(50, 5, 1)
        task = asyncio.create_task(self.hub.stop())
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.assertEqual(command["action"], "stop")
        self.report(ack={"id": command["id"], "ok": True})
        result = await asyncio.wait_for(task, timeout=2)
        self.assertTrue(result["ok"])

    async def test_offline_and_rejected(self):
        with self.assertRaises(ToyBridgeError):
            await self.hub.stop()
        self.report()
        task = asyncio.create_task(self.hub.stop())
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.report(ack={"id": command["id"], "ok": False, "error": "ble not ready"})
        with self.assertRaises(ToyBridgeError) as ctx:
            await task
        self.assertIn("ble not ready", ctx.exception.detail)

    async def test_emergency_lock_blocks_play(self):
        self.report(locked_ms=20000, button=True)
        self.hub.set_remote(True, 600)
        with self.assertRaises(ToyBridgeError) as ctx:
            await self.hub.run(50, 5, 1)
        self.assertIn("急停", ctx.exception.detail)
        self.assertIsNotNone(self.hub.snapshot()["button_pressed_s_ago"])

    async def test_new_command_supersedes_undelivered(self):
        self.report()
        first = asyncio.create_task(self.hub.stop())
        await asyncio.sleep(0)
        second = asyncio.create_task(self.hub.stop())
        await asyncio.sleep(0)
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        with self.assertRaises(ToyBridgeError):
            await first
        self.report(ack={"id": command["id"], "ok": True})
        self.assertTrue((await second)["ok"])

    async def test_scan_results_arrive_with_ack(self):
        self.report()
        task = asyncio.create_task(self.hub.scan(1000))
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.assertEqual(command["action"], "scan")
        self.report(
            ack={"id": command["id"], "ok": True},
            scan=[{"addr": "aa:bb:cc:dd:ee:ff", "rssi": -60, "seen": 3, "name": "HJ-002", "mfg": "7100"}],
        )
        result = await asyncio.wait_for(task, timeout=3)
        self.assertEqual(result["scan"]["entries"][0]["name"], "HJ-002")

    async def test_wait_times_out_without_command(self):
        started = time.monotonic()
        self.assertIsNone(await self.hub.wait_for_command(0.2))
        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()


class TwoChannelTests(unittest.IsolatedAsyncioTestCase):
    """两路同时：设备两路各自记状态，一张时间表排完「谁先到点谁先停」。"""

    def setUp(self):
        self.hub = ToyBridgeHub()
        self.hub.report({"fw": "t", "state": "playing", "ble_ready": True})
        self.hub.set_remote(True, 600)

    async def _run(self, channel, level, seconds):
        task = asyncio.create_task(
            self.hub.run(level, seconds, 1, channel=channel, require_remote=False)
        )
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.hub.report({
            "fw": "t", "state": "playing", "ble_ready": True,
            "cmd_id": command["id"], "ack": {"id": command["id"], "ok": True},
        })
        await asyncio.wait_for(task, timeout=2)
        return command

    async def test_second_channel_does_not_stop_the_first(self):
        await self._run("suction", 60, 30)
        command = await self._run("insert", 70, 10)
        modes = [bytes.fromhex(s["frame"])[11] for s in command["steps"]]
        # 吮吸先刷一帧、入体跑、入体先到点只停入体（0x02，不是 0x03）、吮吸接着跑
        self.assertEqual(modes[0], proto.MODE_RUN)
        self.assertEqual(modes[1], proto.INSERT_MODE_RUN)
        self.assertIn(proto.INSERT_MODE_STOP, modes[2:])
        self.assertNotIn(proto.MODE_STOP_BOTH, modes)
        self.assertNotIn(proto.MODE_STOP, modes)
        self.assertEqual(modes[-1], proto.MODE_RUN)
        self.assertGreater(command["ttl_ms"], 29_000)
        names = {c["channel"] for c in self.hub.active_channels()}
        self.assertEqual(names, {"suction", "insert"})

    async def test_longer_new_channel_stops_the_older_one_on_time(self):
        await self._run("insert", 70, 5)
        command = await self._run("suction", 60, 40)
        modes = [bytes.fromhex(s["frame"])[11] for s in command["steps"]]
        self.assertIn(proto.INSERT_MODE_STOP, modes)
        self.assertEqual(modes[-1], proto.MODE_RUN)
        self.assertGreater(command["ttl_ms"], 39_000)

    async def test_stop_clears_both_channels(self):
        await self._run("suction", 60, 30)
        await self._run("insert", 70, 30)
        task = asyncio.create_task(self.hub.stop())
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.hub.report({
            "fw": "t", "state": "idle", "ble_ready": True,
            "cmd_id": command["id"], "ack": {"id": command["id"], "ok": True},
        })
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual(self.hub.active_channels(), [])


class LinkLogTests(unittest.TestCase):
    """固件不汇报复位原因，掉线只能从 uptime 和汇报间隔反推。"""

    def setUp(self):
        self.hub = ToyBridgeHub()

    def report(self, uptime_s, **extra):
        self.hub.report({"fw": "t", "chip": "esp32c3", "state": "idle",
                         "ble_ready": True, "uptime_s": uptime_s, "rssi": -64, **extra})

    def test_uptime_going_backwards_counts_a_reboot(self):
        self.report(600)
        self.report(12)
        log = self.hub.link_log()
        self.assertEqual(log["boots"], 1)
        self.assertEqual(log["events"][0]["kind"], "boot")
        self.assertEqual(log["events"][0]["ran_s"], 600)

    def test_first_report_is_not_a_reboot(self):
        self.report(5)
        self.assertEqual(self.hub.link_log(), {"boots": 0, "drops": 0, "events": []})

    def test_long_silence_counts_a_dropout(self):
        self.report(100)
        self.hub._last_seen -= 90           # 装作中间漏了一分半
        self.report(190)
        log = self.hub.link_log()
        self.assertEqual((log["drops"], log["boots"]), (1, 0))
        self.assertEqual(log["events"][0]["kind"], "gap")
        self.assertGreater(log["events"][0]["seconds"], 60)

    def test_steady_polling_logs_nothing(self):
        for up in range(8, 80, 8):
            self.report(up)
        self.assertEqual(self.hub.link_log()["events"], [])

    def test_online_ttl_tracks_the_shortened_poll(self):
        from shikong import hub as toy_bridge
        self.assertLessEqual(toy_bridge.MAX_POLL_WAIT_SECONDS, 10)
        self.assertGreater(toy_bridge.ONLINE_TTL_SECONDS, toy_bridge.MAX_POLL_WAIT_SECONDS * 2)


class StaleChannelTests(unittest.IsolatedAsyncioTestCase):
    """掉线后「叠加状态新状态不生效」。

    板子掉线超过 15 秒，固件看门狗会自己把两路都停掉；回来之后它汇报 state=idle。
    服务端原来要等「计划的结束时间 + 2 秒」才肯清记录，中间这段时间它以为旧那一路还在跑，
    于是下一条命令被排成「叠加」，把一路没人要的又叫起来，新命令的时间表也跟着歪。
    """

    def setUp(self):
        self.hub = ToyBridgeHub()
        self.hub.report({"fw": "t", "chip": "esp32c3", "ble_ready": True, "state": "idle"})
        self.hub.set_remote(True, 3600)

    def report(self, state="idle", **extra):
        self.hub.report({"fw": "t", "chip": "esp32c3", "ble_ready": True,
                         "state": state, "uptime_s": 100, **extra})

    def seconds_later(self, seconds=5.0):
        """装作过了几秒。发完命令有 3 秒宽限期（板子还没把 playing 报上来），
        不跳过去的话测不到宽限期之后的行为。"""
        self.hub._last_play_at -= seconds

    async def start(self, channel, seconds, level=50):
        task = asyncio.create_task(
            self.hub.run(level, seconds, 1, channel=channel, require_remote=True))
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.assertIsNotNone(command, "命令没进槽位")
        self.report(state="playing", cmd_id=command["id"],
                    ack={"id": command["id"], "ok": True})
        await asyncio.wait_for(task, timeout=2)
        return command

    async def test_board_reporting_idle_early_clears_the_record(self):
        await self.start(proto.CHANNEL_SUCTION, 120)
        self.assertIn(proto.CHANNEL_SUCTION, self.hub._channels)
        self.seconds_later()
        self.report(state="idle", stops=1)          # 看门狗停的，远没到 120 秒
        self.assertEqual(self.hub._channels, {})
        self.assertEqual(self.hub.active_channels(), [])

    async def test_selfstop_shows_up_in_the_link_log(self):
        await self.start(proto.CHANNEL_SUCTION, 120)
        self.report(state="playing", stops=0)
        self.seconds_later()
        self.report(state="idle", stops=1)
        events = self.hub.link_log()["events"]
        self.assertTrue(events and events[0]["kind"] == "selfstop", events)
        self.assertGreater(events[0]["early_s"], 100)

    async def test_next_command_does_not_resurrect_a_stopped_channel(self):
        await self.start(proto.CHANNEL_SUCTION, 120)
        self.seconds_later()
        self.report(state="idle", stops=1)          # 吮吸已经被板子停了
        task = asyncio.create_task(
            self.hub.run(40, 10, 1, channel=proto.CHANNEL_INSERT, require_remote=True))
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        suction_run = proto.frame_hex(0, proto.MODE_RUN, channel=proto.CHANNEL_SUCTION)[:6]
        for step in command["steps"]:
            self.assertFalse(step["frame"].startswith(suction_run),
                             "吮吸被重新叫起来了：%r" % (command["steps"],))
        self.assertLess(command["ttl_ms"], 20000, "时间表被旧那一路的结束时间拉长了")
        self.report(state="playing", cmd_id=command["id"],
                    ack={"id": command["id"], "ok": True})
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual([c["channel"] for c in self.hub.active_channels()],
                         [proto.CHANNEL_INSERT])

    async def test_still_interleaves_while_the_board_really_plays(self):
        await self.start(proto.CHANNEL_SUCTION, 120)
        self.report(state="playing")                 # 真的在跑，叠加照旧
        task = asyncio.create_task(
            self.hub.run(40, 10, 1, channel=proto.CHANNEL_INSERT, require_remote=True))
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.report(state="playing", cmd_id=command["id"],
                    ack={"id": command["id"], "ok": True})
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual(
            sorted(c["channel"] for c in self.hub.active_channels()),
            sorted([proto.CHANNEL_SUCTION, proto.CHANNEL_INSERT]))

    async def test_failed_switch_keeps_the_other_channel_on_record(self):
        await self.start(proto.CHANNEL_SUCTION, 120)
        self.report(state="playing")
        # 叠加一条波浪，这次让它 ack 失败
        task = asyncio.create_task(
            self.hub.run(40, 10, 2, channel=proto.CHANNEL_INSERT, require_remote=True))
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.report(state="playing", cmd_id=command["id"],
                    ack={"id": command["id"], "ok": False, "error": "ble not ready"})
        with self.assertRaises(ToyBridgeError):
            await asyncio.wait_for(task, timeout=2)
        self.assertIn(proto.CHANNEL_SUCTION, self.hub._channels,
                      "命令没发成功就把对方的记录删了")


class AnyModeTwoChannelTests(unittest.IsolatedAsyncioTestCase):
    """两路同时跑不限于「都是恒定」：任何模式组合都要两路各跑各的。"""

    def setUp(self):
        self.hub = ToyBridgeHub()
        self.hub.report({"fw": "t", "state": "playing", "ble_ready": True})
        self.hub.set_remote(True, 600)

    def ack(self, command, ok=True):
        self.hub.report({
            "fw": "t", "state": "playing", "ble_ready": True,
            "cmd_id": command["id"], "ack": {"id": command["id"], "ok": ok},
        })

    async def _run(self, channel, level, seconds, mode):
        task = asyncio.create_task(
            self.hub.run(level, seconds, mode, channel=channel, require_remote=False))
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.ack(command)
        result = await asyncio.wait_for(task, timeout=2)
        return command, result

    @staticmethod
    def frames(command):
        return [bytes.fromhex(s["frame"]) for s in command["steps"]]

    def assert_both_run_without_killing(self, command):
        frames = self.frames(command)
        modes = [f[11] for f in frames]
        self.assertFalse(command["loop"])
        self.assertIn(proto.MODE_RUN, modes)
        self.assertIn(proto.INSERT_MODE_RUN, modes)
        self.assertNotIn(proto.MODE_STOP_BOTH, modes, "表里出现了两路一起停")
        self.assertLessEqual(len(frames), proto.MAX_STEPS)
        self.assertEqual(modes[0], proto.MODE_RUN, "先在跑的那一路要先补一帧，不能先停它")

    async def test_constant_then_pulse_keeps_both(self):
        # AI 实际最常这样下：吮吸恒定 150 秒 + 入体脉冲 150 秒
        await self._run("suction", 60, 150, 1)
        command, result = await self._run("insert", 58, 150, 3)
        self.assert_both_run_without_killing(command)
        self.assertIn("同时", result["summary"])
        self.assertGreater(command["ttl_ms"], 149_000)
        self.assertEqual({c["channel"] for c in self.hub.active_channels()}, {"suction", "insert"})

    async def test_pulse_then_pulse_keeps_both(self):
        await self._run("suction", 52, 120, 3)
        command, _ = await self._run("insert", 45, 120, 3)
        self.assert_both_run_without_killing(command)
        insert_levels = {f[4] for f in self.frames(command) if f[11] == proto.INSERT_MODE_RUN}
        self.assertEqual(insert_levels, {proto.user_to_device(45, "insert"), proto.user_to_device(1, "insert")})
        suction_levels = {f[2] for f in self.frames(command) if f[11] == proto.MODE_RUN}
        self.assertEqual(suction_levels, {proto.user_to_device(52), proto.user_to_device(1)})

    async def test_shorter_channel_stops_alone_and_the_other_carries_on(self):
        await self._run("insert", 70, 60, 3)
        command, _ = await self._run("suction", 40, 20, 2)
        modes = [f[11] for f in self.frames(command)]
        cut = modes.index(proto.MODE_STOP)          # 吮吸先到点：只停吮吸
        self.assertNotIn(proto.INSERT_MODE_STOP, modes)
        self.assertNotIn(proto.MODE_RUN, modes[cut + 1:], "吮吸停了之后又被叫起来")
        self.assertEqual(modes[cut + 2], proto.INSERT_MODE_RUN, "停完吮吸要给入体补一帧")
        elapsed = sum(s["ms"] for s in command["steps"][:cut])
        self.assertAlmostEqual(elapsed / 1000, 20, delta=1.0)
        self.assertAlmostEqual(sum(s["ms"] for s in command["steps"]) / 1000, 60, delta=1.5)

    async def test_running_channel_keeps_its_own_rhythm(self):
        await self._run("suction", 80, 60, 3)        # 3 秒强、3 秒轻
        self.hub._channels["suction"]["t0"] -= 4.0   # 吮吸已经跑了 4 秒，正在「轻」那半拍
        command, _ = await self._run("insert", 30, 30, 1)
        first = self.frames(command)[0]
        self.assertEqual(first[11], proto.MODE_RUN)
        self.assertEqual(first[2], proto.user_to_device(1), "叠上去时吮吸被拉回了开头")
        self.assertAlmostEqual(command["steps"][0]["ms"] + command["steps"][1]["ms"], 2000, delta=300)

    async def test_climb_actually_climbs_when_layered(self):
        await self._run("insert", 50, 60, 1)
        command, _ = await self._run("suction", 90, 20, 4)
        levels = [f[2] for f in self.frames(command) if f[11] == proto.MODE_RUN]
        self.assertGreater(len(set(levels)), 10)
        self.assertEqual(levels, sorted(levels))

    async def test_wave_pair_is_planned_in_windows(self):
        await self._run("suction", 48, 150, 2)
        command, _ = await self._run("insert", 32, 120, 2)
        self.assert_both_run_without_killing(command)
        self.assertIsNotNone(self.hub._replan_task, "装不下全程却没安排接着排")
        self.assertLess(command["ttl_ms"], 120_000)
        # 不等真的过二十几秒：直接让接续那一段现在跑
        self.hub._replan_task.cancel()
        gen = self.hub._gen
        replan = asyncio.create_task(self.hub._replan_later(0, gen))
        nxt = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.assertNotEqual(nxt["id"], command["id"])
        self.assert_both_run_without_killing(nxt)
        self.ack(nxt)
        await asyncio.wait_for(replan, timeout=3)
        self.assertEqual(len(self.hub.history()), 2, "接续排表不该刷进命令记录")

    async def test_stop_cancels_the_follow_up_window(self):
        await self._run("suction", 48, 150, 2)
        await self._run("insert", 32, 120, 2)
        pending = self.hub._replan_task
        task = asyncio.create_task(self.hub.stop())
        command = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.ack(command)
        await asyncio.wait_for(task, timeout=2)
        await asyncio.sleep(0)
        self.assertTrue(pending.cancelled() or pending.done())
        self.assertIsNone(self.hub._replan_task)

    async def test_two_runs_fired_together_both_land(self):
        # 同一轮两个 tool call 并发：第二条要等第一条 ack 之后、带着它一起排
        first = asyncio.create_task(self.hub.run(60, 150, 1, channel="suction", require_remote=False))
        second = asyncio.create_task(self.hub.run(58, 150, 3, channel="insert", require_remote=False))
        c1 = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.ack(c1)
        c2 = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.ack(c2)
        r1, r2 = await asyncio.wait_for(asyncio.gather(first, second), timeout=3)
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assert_both_run_without_killing(c2)
        self.assertEqual({c["channel"] for c in self.hub.active_channels()}, {"suction", "insert"})

    async def test_stop_while_a_run_waits_for_ack_wins(self):
        task = asyncio.create_task(self.hub.run(60, 150, 2, channel="suction", require_remote=False))
        c1 = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        stopper = asyncio.create_task(self.hub.stop())
        await asyncio.sleep(0)
        self.ack(c1)
        await asyncio.wait_for(task, timeout=2)
        c2 = await asyncio.wait_for(self.hub.wait_for_command(2), timeout=2)
        self.assertEqual(c2["action"], "stop")
        self.ack(c2)
        await asyncio.wait_for(stopper, timeout=2)
        self.assertEqual(self.hub._channels, {})
        self.assertIsNone(self.hub._replan_task)


class ResetReasonTests(unittest.TestCase):
    """码表必须和 esp_system.h 的 esp_reset_reason_t 对齐。

    这里错位过一格（掉电写成 10、USB 写成 14），刷完机板子报 11，面板显示成「SDIO」。
    这张表是掉线排查唯一的事实来源，错一格就会把「供电不够」看成别的问题。
    """

    # 照抄 esp-idf/components/esp_system/include/esp_system.h 的枚举顺序
    IDF_ORDER = [
        "UNKNOWN", "POWERON", "EXT", "SW", "PANIC", "INT_WDT", "TASK_WDT", "WDT",
        "DEEPSLEEP", "BROWNOUT", "SDIO", "USB", "JTAG", "EFUSE", "PWR_GLITCH", "CPU_LOCKUP",
    ]
    KEYWORD = {
        "POWERON": "上电", "SW": "软件", "PANIC": "崩溃", "INT_WDT": "中断看门狗",
        "TASK_WDT": "任务看门狗", "DEEPSLEEP": "深睡", "BROWNOUT": "掉电",
        "SDIO": "SDIO", "USB": "USB", "JTAG": "JTAG", "CPU_LOCKUP": "CPU",
    }

    def test_every_code_is_covered(self):
        from shikong.hub import RESET_REASONS
        self.assertEqual(sorted(RESET_REASONS), list(range(len(self.IDF_ORDER))))

    def test_each_code_says_the_right_thing(self):
        from shikong.hub import RESET_REASONS
        for code, name in enumerate(self.IDF_ORDER):
            if name in self.KEYWORD:
                self.assertIn(self.KEYWORD[name], RESET_REASONS[code],
                              "码 %d 应该是 %s，现在写的是 %r" % (code, name, RESET_REASONS[code]))

    def test_brownout_is_nine_and_usb_is_eleven(self):
        from shikong.hub import RESET_REASONS
        self.assertIn("掉电", RESET_REASONS[9])
        self.assertIn("USB", RESET_REASONS[11])
        self.assertNotIn("掉电", RESET_REASONS[10])

    def test_wifi_reasons_match_the_idf_enum(self):
        """照抄 esp_wifi_types_generic.h 的 wifi_err_reason_t。"""
        from shikong.hub import WIFI_DISC_REASONS
        expect = {
            1: "未指明", 2: "认证过期", 4: "踢", 8: "走", 15: "握手",
            200: "beacon", 201: "找不到", 202: "认证失败", 203: "关联失败",
            204: "握手", 207: "漫游",
        }
        for code, keyword in expect.items():
            self.assertIn(code, WIFI_DISC_REASONS, "缺了 reason %d" % code)
            self.assertIn(keyword, WIFI_DISC_REASONS[code],
                          "reason %d 写的是 %r" % (code, WIFI_DISC_REASONS[code]))
        # 4 是「被老化踢掉」，是这类掉线最常见的码，不能和「漫游」搞混
        self.assertNotIn("漫游", WIFI_DISC_REASONS[4])
