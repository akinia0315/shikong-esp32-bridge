"""ESP32 桥的服务端：板子长轮询这里拿时间表，你（或你的 AI）通过 HTTP 下指令。

    SHIKONG_BRIDGE_TOKEN=随便一串长随机字符 python -m shikong.server --host 0.0.0.0 --port 8765

- 板子：POST /api/toy/bridge/poll，请求头 X-Toy-Bridge-Token 必须等于 SHIKONG_BRIDGE_TOKEN。
- 控制接口（/api/toy/state、/api/toy/command 等）：设了 SHIKONG_CONTROL_TOKEN 就要带
  Authorization: Bearer <它>；没设就只接受本机（127.0.0.1）来的请求。
"""

from __future__ import annotations

import argparse
import hmac
import logging
import os
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import hub as hub_module
from .hub import ToyBridgeError, toy_hub

BRIDGE_TOKEN_HEADER = "X-Toy-Bridge-Token"

app = FastAPI(title="shikong-esp32-bridge")


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


async def require_bridge(x_toy_bridge_token: str = Header(default="")) -> None:
    if not _same(x_toy_bridge_token, os.environ.get("SHIKONG_BRIDGE_TOKEN", "")):
        raise HTTPException(status_code=401, detail="bad bridge token")


async def require_control(request: Request, authorization: str = Header(default="")) -> None:
    expected = os.environ.get("SHIKONG_CONTROL_TOKEN", "")
    if expected:
        if not _same(authorization.removeprefix("Bearer ").strip(), expected):
            raise HTTPException(status_code=401, detail="bad control token")
        return
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="set SHIKONG_CONTROL_TOKEN to control from other hosts")


class RemoteReq(BaseModel):
    enabled: bool
    minutes: float = Field(default=60, ge=1, le=12 * 60)


class CommandReq(BaseModel):
    action: str                         # run / stop / scan / raw / reboot
    channel: str = "suction"            # suction / insert
    level: int = 50
    duration_seconds: float = 10
    mode: int = 1                       # 1 恒定 / 2 波浪 / 3 脉冲 / 4 攀升
    require_remote: bool = False        # 给 AI 用的入口设 true：远控窗口没开就拒绝
    frames: list[str] = Field(default_factory=list)
    ms: int = 2500
    active: bool = False


@app.post("/api/toy/bridge/poll", dependencies=[Depends(require_bridge)])
async def bridge_poll(request: Request, wait: int = 20) -> dict[str, Any]:
    """板子先汇报状态（含上一条命令的 ack），再挂起等命令。"""
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    toy_hub.report(payload if isinstance(payload, dict) else {})
    command = await toy_hub.wait_for_command(max(0, min(wait, hub_module.MAX_POLL_WAIT_SECONDS)))
    return {"ok": True, "cmd": command, "watchdog_ms": toy_hub.watchdog_ms, "server_time": int(time.time())}


@app.get("/api/toy/state", dependencies=[Depends(require_control)])
async def state() -> dict[str, Any]:
    return {"state": toy_hub.snapshot(), "history": toy_hub.history()}


@app.get("/api/toy/scan", dependencies=[Depends(require_control)])
async def last_scan() -> dict[str, Any]:
    return toy_hub.last_scan()


@app.post("/api/toy/remote", dependencies=[Depends(require_control)])
async def remote(req: RemoteReq) -> dict[str, Any]:
    return {"state": toy_hub.set_remote(req.enabled, req.minutes * 60)}


@app.post("/api/toy/command", dependencies=[Depends(require_control)])
async def command(req: CommandReq) -> dict[str, Any]:
    try:
        if req.action == "run":
            return await toy_hub.run(
                req.level, req.duration_seconds, req.mode,
                channel=req.channel, require_remote=req.require_remote,
            )
        if req.action == "stop":
            return await toy_hub.stop()
        if req.action == "raw":
            return await toy_hub.play_raw(req.frames, req.ms)
        if req.action == "scan":
            return await toy_hub.scan(req.ms, req.active)
        if req.action == "reboot":
            return await toy_hub.reboot()
    except ToyBridgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    raise HTTPException(status_code=422, detail="unknown action")


def main() -> None:
    parser = argparse.ArgumentParser(description="失控 4.0 ESP32 桥服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not os.environ.get("SHIKONG_BRIDGE_TOKEN"):
        raise SystemExit("先设 SHIKONG_BRIDGE_TOKEN（板子配网页里填同一串）")
    logging.basicConfig(level=logging.INFO)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
