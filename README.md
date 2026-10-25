# shikong-esp32-bridge

**诱桃「失控 4.0」（蓝牙名 HJ-002）吮吸 + 入体两路的广播协议，和一块 ESP32 开发板做的远程桥。**

> 站在 [Moonriseandset/cachito-shikong-mcp](https://github.com/Moonriseandset/cachito-shikong-mcp) 的肩膀上。
> 「失控 4.0 不走 GATT、走 BLE 广播」以及吮吸那一路的帧格式，都是那个仓库逆出来的，请先去给它点个星。

这个仓库在它的基础上补了三件事：

1. **入体那一路逆出来了。** 运行帧、停止帧、反着的档位字节、两路分开停的掩码，见 [docs/protocol.md](docs/protocol.md)。
   顺带发现 byte8-9 不是固定值而是**会话号**：玩具被官方小程序连过之后只认小程序的号，两路突然都不动大多是这个原因。
2. **两路可以同时跑，互不打断。** 设备两路各自记状态，所以「同时」是把两路各自的换档时刻合进一张时间表。
   恒定、波浪、脉冲、攀升随便组合，谁先到点只停谁，另一路接着跑（[shikong/schedule.py](shikong/schedule.py)）。
3. **不用 Mac、不用安卓机，只用一块几块钱的 ESP32 开发板就能逆向和远控。**
   板子既是抄帧用的嗅探器（被动扫描官方小程序的广播），也是长期插在家里的广播器，
   服务器在哪儿都行。思路和踩过的坑写在 [docs/reverse-engineering.md](docs/reverse-engineering.md)。

**有 Mac 的人也能直接用。** 协议和合排都是纯 Python，`mac/play.py` 用 CoreBluetooth 广播，
或者把 `shikong.proto` 接进原仓库的 MCP，见下面「Mac 当桥」。

```
失控 4.0 ◄── BLE 广播 ── ESP32-C3 开发板 ── Wi‑Fi 长轮询 ──► 你的服务器（shikong.server）◄── HTTP ── 你 / 你的 AI
                         （放在玩具旁边）                    （家里电脑、NAS、VPS 都行）
```

## 硬件

| | |
|---|---|
| **实测** | **ESP32-C3 SuperMini**（QFN32，4 MB 内置 flash，原生 USB，板载 BOOT 键）|
| 也编了（没上过真机）| ESP32-C2，晶振 26 MHz / 40 MHz 各一份 |
| 其它 | 一根能传数据的 USB-C 线、一个 USB 充电头 |

C3 SuperMini 小板满功率发射会掉电重启，固件把 Wi‑Fi 发射功率压到了 8.5 dBm。
板子离路由器远一点就容易掉线，放近一点最管用。细节见 [docs/hardware.md](docs/hardware.md)。

## 快速开始（ESP32 桥）

**1. 起服务端**（Python 3.10+）

```bash
pip install -r requirements.txt
export SHIKONG_BRIDGE_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
export SHIKONG_CONTROL_TOKEN=另一串随机字符     # 不设的话控制接口只接受本机请求
python -m shikong.server --host 0.0.0.0 --port 8765
```

板子要能访问到这个地址：同一个局域网填 `http://电脑IP:8765`，放公网就套一层 HTTPS 反代。

**2. 刷板子**：去 [Releases](../../releases) 下载固件，把 `tools/flash.py` 和 `.bin` 放进同一个文件夹，
运行 `python3 flash.py`，跟着提示走。它会自己认芯片、选固件。也可以手动刷：

```bash
python -m esptool --chip esp32c3 --port <串口> write-flash 0x0 shikong-bridge-esp32c3.bin
```

**3. 配网**：板子第一次上电会开一个没有密码的 Wi‑Fi `Shikong-Bridge`。手机连上后打开 `http://192.168.4.1/`，
填家里的 2.4 GHz Wi‑Fi、服务器地址、`SHIKONG_BRIDGE_TOKEN`，然后保存。板子重启后会自己连上服务器。

**4. 用**

```bash
H="Authorization: Bearer $SHIKONG_CONTROL_TOKEN"
curl -H "$H" localhost:8765/api/toy/state                       # online: true 就是连上了
curl -H "$H" localhost:8765/api/toy/command -H 'content-type: application/json' \
     -d '{"action":"run","channel":"suction","level":60,"duration_seconds":30,"mode":1}'
curl -H "$H" localhost:8765/api/toy/command -H 'content-type: application/json' \
     -d '{"action":"run","channel":"insert","level":40,"duration_seconds":20,"mode":3}'   # 和吮吸同时跑
curl -H "$H" localhost:8765/api/toy/command -H 'content-type: application/json' -d '{"action":"stop"}'
```

| 参数 | |
|---|---|
| `channel` | `suction` 吮吸 / `insert` 入体 |
| `level` | 1–100 |
| `duration_seconds` | 0.5–300，到点自动停 |
| `mode` | 1 恒定 / 2 波浪（十秒一个来回）/ 3 脉冲（三秒强三秒轻）/ 4 攀升 |

同一路的新指令会接替旧的，另一路不受影响。

**给 AI 用**：把 `/api/toy/command` 包成工具时带上 `"require_remote": true`。
这样只有你先 `POST /api/toy/remote {"enabled":true,"minutes":60}` 打开远控窗口，AI 的指令才会生效。
`stop` 永远不需要解锁。

**安全**：每条指令都有时长上限，到点板子自己停。板子和服务器断联超过 15 秒也会自己全停。
**短按板上 BOOT 键是急停**，之后 30 秒内不接受新指令；长按 6 秒清空配置。

## Mac 当桥

```bash
pip3 install pyobjc-framework-CoreBluetooth
python3 mac/play.py suction:60:30:1                       # 通道:强度:秒数:模式
python3 mac/play.py suction:60:150:1 insert:58:150:3      # 两路同时
```

`mac/play.py` 的广播部分照着原仓库的 `terminal_test.py` 写，**我们没有 Mac，所以它没在实机上跑过**。
帧和时间表都在 ESP32 上验过，有问题欢迎提 issue。

也可以只拿协议去改原仓库的 MCP：

```python
from shikong import proto
frame = proto.build_frame(proto.user_to_device(40, "insert"), proto.INSERT_MODE_RUN, channel="insert")
uuid_str = proto.cb_uuid(frame)          # 直接喂给 CBUUID.UUIDWithString_
```

或者用命令行把指令展开成时间表 JSON（每一步都附了 CoreBluetooth UUID 字符串）：

```bash
python -m shikong plan suction:60:20:1 insert:40:10:3
```

**注意会话号**：原仓库写的 `27 C6` 只在玩具刚上电、没被官方小程序连过时有效。
玩具被小程序连过以后要换成小程序的号（`proto.SESSION_ID`，我们这台是 `44 52`）。
你那台的号可以用 ESP32 被动扫描一次读出来，见 [docs/reverse-engineering.md](docs/reverse-engineering.md)。

## 目录

```
shikong/proto.py      帧格式（纯标准库）
shikong/schedule.py   两路合排（纯标准库）
shikong/hub.py        ESP32 桥的指令队列、ack、掉线记录
shikong/server.py     FastAPI 服务端
mac/play.py           Mac 当桥
firmware/             ESP32 固件（ESP-IDF v5.5 + NimBLE）
tools/flash.py        一键刷机
tools/decode_frames.py  抄帧解码：按时间排开、标变化字节、查校验、读会话号
samples/              抄到的入体帧（已去掉蓝牙地址）
tests/                python -m unittest discover -s tests -t .
```

## 致谢

- [Moonriseandset/cachito-shikong-mcp](https://github.com/Moonriseandset/cachito-shikong-mcp)：广播控制的发现、吮吸协议、校验和档位映射
- [星轨工坊](https://workshop.star-trail.uk/)：诱桃大秀 3.0 的研究，指出了「走广播不走 GATT」（经原仓库）

## 声明

非官方项目，和诱桃没有任何关系。仅供成年人在自己的设备上使用，风险自负。
只在你自己知情、同意的情况下远控。拿不准的时候，按 BOOT 键或者关掉玩具。

MIT License
