import os
import unittest

try:
    from fastapi.testclient import TestClient
except ImportError:  # 只装了协议部分时跳过
    TestClient = None


@unittest.skipIf(TestClient is None, "没装 fastapi/httpx")
class ServerAuthTests(unittest.TestCase):
    def setUp(self):
        os.environ["SHIKONG_BRIDGE_TOKEN"] = "board-secret"
        os.environ.pop("SHIKONG_CONTROL_TOKEN", None)
        from shikong import server

        self.server = server
        self.client = TestClient(server.app)

    def test_board_needs_the_bridge_token(self):
        self.assertEqual(self.client.post("/api/toy/bridge/poll?wait=0", json={}).status_code, 401)
        r = self.client.post("/api/toy/bridge/poll?wait=0", json={"state": "idle"},
                             headers={"X-Toy-Bridge-Token": "board-secret"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["cmd"])

    def test_control_without_token_is_local_only(self):
        # TestClient 的来源地址是 "testclient"，不算本机
        self.assertEqual(self.client.get("/api/toy/state").status_code, 403)

    def test_control_token(self):
        os.environ["SHIKONG_CONTROL_TOKEN"] = "ctl"
        self.assertEqual(self.client.get("/api/toy/state").status_code, 401)
        r = self.client.get("/api/toy/state", headers={"Authorization": "Bearer ctl"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("state", r.json())

    def tearDown(self):
        os.environ.pop("SHIKONG_CONTROL_TOKEN", None)
