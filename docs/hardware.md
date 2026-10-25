# 硬件和固件

## 开发板

**实测型号：ESP32-C3 SuperMini**（QFN32 封装，4 MB 内置 flash，原生 USB-Serial/JTAG，板载 BOOT 键在 GPIO 9）。
就是网上几块钱一块、拇指大小的那种，插一个 USB 充电头放在玩具附近就行。

- C3 用的是原生 USB，芯片每复位一次，电脑都会响一声断开/连接。**连着一直响，说明固件在反复重启。**
- ESP32-C2 的固件也编了，晶振 26 MHz 和 40 MHz 各一份（C2 不能自动识别晶振）。**C2 没上过真机。**
- 换别的 ESP32 也行：改 `idf.py set-target`，再看一下 `CONFIG_TOY_BUTTON_GPIO` 是不是你板子上的 BOOT 键。

## 已经替你处理掉的坑

- **满功率发射会掉电重启。** 这类小板的供电撑不住 Wi‑Fi 满功率发射，一发射就复位。
  所以发射功率压到了 8.5 dBm（`CONFIG_ESP_PHY_MAX_WIFI_TX_POWER=10` 再加 `esp_wifi_set_max_tx_power(34)`）。
  代价是上行信号弱：我们这块板子插在电脑旁边 rssi −54，插到墙上的插座就只有 −68 到 −72。**容易掉线就把板子挪近路由器。**
  别去调高发射功率，那样只会把「断网」变成「重启」。
- **关掉了 Wi‑Fi 省电模式。** C3 的 Wi‑Fi 和 BLE 共用一个射频，省电模式下容易漏收 beacon、被路由器踢掉。
- **配网页去掉了 Wi‑Fi 扫描。** 开着热点时扫描会把手机踢下线。配网页加了一个迷你 DNS，手机连上热点会自动弹出页面。
- **长轮询**：板子请求挂 20 秒，服务端提前到 8 秒就返回。上行变密之后，路由器不会把板子当成睡死了踢掉，新指令最多等 8 秒。
- HTTP 连接开了 TCP keepalive，断网后大约 20 秒就能发现并重连。
- 每次汇报都带复位原因（`esp_reset_reason`）、掉线次数、最后一次掉线的原因码。服务端 `GET /api/toy/state` 的 `link` 里能看到掉线记录。

## 安全机制

- 每张时间表都带 `ttl_ms`，到点板子自己发全停。
- 板子和服务器断联超过 15 秒（看门狗），自己发全停。
- **短按 BOOT 键是急停**，之后 30 秒内拒绝新指令。**长按 6 秒清空配置**，回到配网热点。

## 自己编译

用 ESP-IDF v5.5（我们用的是 v5.5.5）：

```bash
cd firmware
idf.py -B build_c3 -DSDKCONFIG=sdkconfig.c3 set-target esp32c3
idf.py -B build_c3 -DSDKCONFIG=sdkconfig.c3 build
# 合成一个从 0x0 开始刷的整块镜像
cd build_c3 && python -m esptool --chip esp32c3 merge_bin -o shikong-bridge-esp32c3.bin @flash_args
```

C2 26 MHz 版要多带一份默认配置：
`-DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.esp32c2;sdkconfig.c2-26m.defaults"`。

`*-app-only.bin` 只刷 app 分区（`0x10000`），**Wi‑Fi 配置和令牌都会保留**，升级固件时用它。
`tools/flash.py` 会问你「以前配过网吗」，然后替你选。

## 板子和服务端之间的协议

板子按固定节奏 `POST {服务器}/api/toy/bridge/poll?wait=20`，请求头 `X-Toy-Bridge-Token`。
请求体是状态：`fw` `chip` `uptime_s` `heap` `rssi` `state` `stops` `reset` `wifi_drops` `wifi_reason`，
还会带上一条命令的 `ack`、扫描结果 `scan` / `frames`。
响应里的 `cmd` 是下一条命令：`play`（`steps` `loop` `ttl_ms` `tail`）、`stop`、`scan`、`reboot`，没有命令时是 `null`。
完整字段见 `shikong/hub.py` 和 `firmware/main/bridge_client.c`。
