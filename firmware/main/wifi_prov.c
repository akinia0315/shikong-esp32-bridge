#include "wifi_prov.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "lwip/sockets.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "wifi";
static EventGroupHandle_t s_events;
#define BIT_CONNECTED BIT0
static int s_retries;
static bool s_sta_enabled;
static esp_timer_handle_t s_reconnect_timer;
// 10-09：板子老是「一直在重连」，但固件什么都不往上报，只能靠服务端猜。
// 这两个会跟着每次轮询上去：掉了几次、最后一次的 reason code
// （4=AP 嫌我们睡太久把我们老化掉了，200=没收到 beacon，15=握手超时，201=找不到 AP）。
static uint16_t s_disconnects;
static uint8_t s_last_disc_reason;

// ESP32-C3 SuperMini 一类小板天线差、供电弱：满功率发射会掉电复位，C2/C3 默认压到 8.5 dBm。
// 别为了治掉线在小板上调高——如果掉线其实是供电不够，调高只会从「断网」变成「重启」。
// 经典 ESP32-WROOM-32 开发板有正经模组天线和更扛电流的稳压，默认满功率（见 Kconfig）。
#define WIFI_TX_POWER_QDBM CONFIG_TOY_WIFI_TX_POWER_QDBM

// 省电模式会让芯片在两个 beacon 之间关收音机；C3 的 Wi-Fi 和 BLE 还共用同一个射频，
// 一边广播一边省电，漏 beacon、被 AP 老化踢掉都会变多。插着电源根本不用省这点电。
#define WIFI_POWER_SAVE WIFI_PS_NONE

static void reconnect_cb(void *arg)
{
    esp_wifi_connect();
}

static void sta_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (s_sta_enabled) {
            esp_wifi_connect();
        }
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_events, BIT_CONNECTED);
        wifi_event_sta_disconnected_t *dc = data;
        if (dc) {
            s_last_disc_reason = dc->reason;
        }
        s_disconnects++;
        if (!s_sta_enabled) {
            return;
        }
        int delay_ms = s_retries < 5 ? 500 : 2000;
        s_retries++;
        ESP_LOGW(TAG, "disconnected reason=%d (total %u), retry %d in %d ms",
                 dc ? dc->reason : -1, (unsigned)s_disconnects, s_retries, delay_ms);
        esp_timer_stop(s_reconnect_timer);
        esp_timer_start_once(s_reconnect_timer, (uint64_t)delay_ms * 1000);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_START) {
        ESP_LOGI(TAG, "AP started");
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STACONNECTED) {
        ESP_LOGI(TAG, "a phone joined the provisioning AP");
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&ev->ip_info.ip));
        s_retries = 0;
        xEventGroupSetBits(s_events, BIT_CONNECTED);
    }
}

static void common_init(void)
{
    if (!s_events) {
        s_events = xEventGroupCreate();
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, sta_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, sta_event, NULL));
    const esp_timer_create_args_t targs = { .callback = reconnect_cb, .name = "wifi_reconnect" };
    ESP_ERROR_CHECK(esp_timer_create(&targs, &s_reconnect_timer));
}

static void apply_tx_power(void)
{
    esp_err_t err = esp_wifi_set_max_tx_power(WIFI_TX_POWER_QDBM);
    int8_t now = 0;
    esp_wifi_get_max_tx_power(&now);
    ESP_LOGI(TAG, "wifi tx power set to %.2f dBm (%s)", now / 4.0, esp_err_to_name(err));
}

void wifi_sta_start(const toy_config_t *cfg)
{
    common_init();
    esp_netif_create_default_wifi_sta();
    wifi_config_t wc = { 0 };
    strncpy((char *)wc.sta.ssid, cfg->ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, cfg->pass, sizeof(wc.sta.password) - 1);
    wc.sta.threshold.authmode = cfg->pass[0] ? WIFI_AUTH_WPA_PSK : WIFI_AUTH_OPEN;
    wc.sta.pmf_cfg.capable = true;
    wc.sta.pmf_cfg.required = false;
    s_sta_enabled = true;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_POWER_SAVE));
    ESP_ERROR_CHECK(esp_wifi_start());
    apply_tx_power();
}

int wifi_disconnect_count(void)
{
    return s_disconnects;
}

int wifi_last_disconnect_reason(void)
{
    return s_last_disc_reason;
}

bool wifi_wait_connected(uint32_t ms)
{
    return (xEventGroupWaitBits(s_events, BIT_CONNECTED, pdFALSE, pdTRUE, pdMS_TO_TICKS(ms)) & BIT_CONNECTED) != 0;
}

bool wifi_is_connected(void)
{
    return (xEventGroupGetBits(s_events) & BIT_CONNECTED) != 0;
}

int wifi_rssi(void)
{
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        return ap.rssi;
    }
    return 0;
}

// ------------------------------------------------------------ provisioning

static toy_config_t *s_cfg;

static const char PAGE_HEAD[] =
    "<!doctype html><html lang=zh><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>失控桥 · 配置</title><style>"
    "body{font:16px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:24px 16px;background:#f6f1ea;color:#3a332c}"
    "h1{font-size:20px;margin:0 0 4px}p{margin:4px 0 16px;color:#7a6f63}"
    "label{display:block;margin:14px 0 4px;font-weight:600}"
    "input,select{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #d8cfc3;border-radius:10px;font-size:16px;background:#fff}"
    "button{margin-top:22px;width:100%;padding:12px;border:0;border-radius:12px;background:#b8865b;color:#fff;font-size:17px}"
    "small{color:#9a8f82}</style></head><body>";

static const char PAGE_FORM[] =
    "<h1>失控桥</h1><p>填好 Wi‑Fi 和服务器令牌，保存后板子会重启并自动连线。</p>"
    "<form method=post action=/save>"
    "<label>Wi‑Fi 名称（2.4 GHz）</label><input name=ssid list=wifis value='%s' required autocomplete=off><datalist id=wifis></datalist>"
    "<label>Wi‑Fi 密码</label><input name=pass type=password value='%s'>"
    "<label>服务器地址</label><input name=url value='%s' required>"
    "<label>桥接令牌（服务端的 SHIKONG_BRIDGE_TOKEN）</label><input name=token value='%s' required autocomplete=off>"
    "<button type=submit>保存并重启</button></form>"
    "<p><small>按住板上 BOOT 键 6 秒可清空配置回到这个页面；短按 BOOT 是急停。</small></p>"
    "</body></html>";

static void html_escape(const char *in, char *out, size_t cap)
{
    size_t n = 0;
    for (; *in && n + 6 < cap; in++) {
        if (*in == '\'') { n += snprintf(out + n, cap - n, "&#39;"); }
        else if (*in == '&') { n += snprintf(out + n, cap - n, "&amp;"); }
        else if (*in == '<') { n += snprintf(out + n, cap - n, "&lt;"); }
        else { out[n++] = *in; }
    }
    out[n] = '\0';
}

static esp_err_t root_get(httpd_req_t *req)
{
    char ssid[TOY_SSID_MAX * 6], url[TOY_URL_MAX * 6], token[TOY_TOKEN_MAX * 6];
    html_escape(s_cfg->ssid, ssid, sizeof(ssid));
    html_escape(s_cfg->url, url, sizeof(url));
    html_escape(s_cfg->token, token, sizeof(token));
    size_t cap = sizeof(PAGE_FORM) + sizeof(ssid) + sizeof(url) + sizeof(token) + TOY_PASS_MAX;
    char *body = malloc(cap);
    if (!body) {
        return httpd_resp_send_500(req);
    }
    snprintf(body, cap, PAGE_FORM, ssid, "", url, token);
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send_chunk(req, PAGE_HEAD, HTTPD_RESP_USE_STRLEN);
    httpd_resp_send_chunk(req, body, HTTPD_RESP_USE_STRLEN);
    httpd_resp_send_chunk(req, NULL, 0);
    free(body);
    return ESP_OK;
}

static esp_err_t wifis_get(httpd_req_t *req)
{
    wifi_ap_record_t *recs = calloc(16, sizeof(wifi_ap_record_t));
    uint16_t n = 16;
    char *out = malloc(1024);
    if (!recs || !out) {
        free(recs); free(out);
        return httpd_resp_send_500(req);
    }
    if (esp_wifi_scan_start(NULL, true) == ESP_OK) {
        esp_wifi_scan_get_ap_records(&n, recs);
    } else {
        n = 0;
    }
    size_t len = 0;
    out[len++] = '[';
    for (int i = 0; i < n; i++) {
        const char *ssid = (const char *)recs[i].ssid;
        if (!ssid[0] || strchr(ssid, '"')) {
            continue;
        }
        if (len + strlen(ssid) + 4 >= 1023) {
            break;
        }
        len += snprintf(out + len, 1024 - len, "%s\"%s\"", len > 1 ? "," : "", ssid);
    }
    out[len++] = ']';
    out[len] = '\0';
    httpd_resp_set_type(req, "application/json");
    httpd_resp_send(req, out, len);
    free(recs);
    free(out);
    return ESP_OK;
}

static void url_decode(const char *in, char *out, size_t cap)
{
    size_t n = 0;
    for (; *in && n + 1 < cap; in++) {
        if (*in == '+') {
            out[n++] = ' ';
        } else if (*in == '%' && in[1] && in[2]) {
            char hex[3] = { in[1], in[2], 0 };
            out[n++] = (char)strtol(hex, NULL, 16);
            in += 2;
        } else {
            out[n++] = *in;
        }
    }
    out[n] = '\0';
}

static void form_field(const char *body, const char *key, char *out, size_t cap)
{
    out[0] = '\0';
    size_t klen = strlen(key);
    const char *p = body;
    while (p && *p) {
        if (strncmp(p, key, klen) == 0 && p[klen] == '=') {
            const char *v = p + klen + 1;
            const char *end = strchr(v, '&');
            size_t vlen = end ? (size_t)(end - v) : strlen(v);
            char *raw = malloc(vlen + 1);
            if (!raw) {
                return;
            }
            memcpy(raw, v, vlen);
            raw[vlen] = '\0';
            url_decode(raw, out, cap);
            free(raw);
            return;
        }
        p = strchr(p, '&');
        if (p) {
            p++;
        }
    }
}

static void trim(char *s)
{
    size_t n = strlen(s);
    while (n && (s[n - 1] == ' ' || s[n - 1] == '\r' || s[n - 1] == '\n')) {
        s[--n] = '\0';
    }
    while (*s == ' ') {
        memmove(s, s + 1, strlen(s));
    }
}

static void strip_trailing_slash(char *s)
{
    size_t n = strlen(s);
    while (n && s[n - 1] == '/') {
        s[--n] = '\0';
    }
}

static esp_err_t save_post(httpd_req_t *req)
{
    if (req->content_len > 1024) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "too long");
    }
    char *body = calloc(1, req->content_len + 1);
    if (!body) {
        return httpd_resp_send_500(req);
    }
    int got = 0;
    while (got < (int)req->content_len) {
        int r = httpd_req_recv(req, body + got, req->content_len - got);
        if (r <= 0) {
            free(body);
            return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "read failed");
        }
        got += r;
    }
    toy_config_t next = { 0 };
    form_field(body, "ssid", next.ssid, TOY_SSID_MAX);
    form_field(body, "pass", next.pass, TOY_PASS_MAX);
    form_field(body, "url", next.url, TOY_URL_MAX);
    form_field(body, "token", next.token, TOY_TOKEN_MAX);
    free(body);
    trim(next.ssid); trim(next.url); trim(next.token);
    strip_trailing_slash(next.url);
    if (!config_store_is_complete(&next) || strncmp(next.url, "http", 4) != 0) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "missing field");
    }
    if (!config_store_save(&next)) {
        return httpd_resp_send_500(req);
    }
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send_chunk(req, PAGE_HEAD, HTTPD_RESP_USE_STRLEN);
    httpd_resp_send_chunk(req, "<h1>已保存</h1><p>板子正在重启并连接 Wi‑Fi。接下来去服务端看它有没有上线（GET /api/toy/state）。</p></body></html>", HTTPD_RESP_USE_STRLEN);
    httpd_resp_send_chunk(req, NULL, 0);
    ESP_LOGI(TAG, "config saved, rebooting");
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
    return ESP_OK;
}

static void dns_task(void *arg)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        vTaskDelete(NULL);
        return;
    }
    struct sockaddr_in bind_addr = { .sin_family = AF_INET, .sin_port = htons(53), .sin_addr.s_addr = htonl(INADDR_ANY) };
    if (bind(sock, (struct sockaddr *)&bind_addr, sizeof(bind_addr)) < 0) {
        ESP_LOGW(TAG, "dns bind failed");
        close(sock);
        vTaskDelete(NULL);
        return;
    }
    uint8_t buf[512];
    while (true) {
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        int len = recvfrom(sock, buf, sizeof(buf) - 16, 0, (struct sockaddr *)&from, &from_len);
        if (len < 12) {
            continue;
        }
        // 找到 question 的结尾（name + QTYPE + QCLASS）
        int pos = 12;
        while (pos < len && buf[pos] != 0) {
            pos += buf[pos] + 1;
        }
        pos += 5;
        if (pos > len) {
            continue;
        }
        buf[2] = 0x81; buf[3] = 0x80;          // 标准应答，无错误
        buf[6] = 0; buf[7] = 1;                // 1 条 answer
        buf[8] = buf[9] = buf[10] = buf[11] = 0;
        const uint8_t answer[] = { 0xC0, 0x0C, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00, 0x3C, 0x00, 0x04, 192, 168, 4, 1 };
        memcpy(buf + pos, answer, sizeof(answer));
        sendto(sock, buf, pos + sizeof(answer), 0, (struct sockaddr *)&from, from_len);
    }
}

void wifi_provisioning_run(toy_config_t *cfg)
{
    s_cfg = cfg;
    common_init();
    esp_netif_create_default_wifi_ap();
    esp_netif_create_default_wifi_sta();
    wifi_config_t ap = { 0 };
    strncpy((char *)ap.ap.ssid, CONFIG_TOY_AP_SSID, sizeof(ap.ap.ssid) - 1);
    ap.ap.ssid_len = strlen(CONFIG_TOY_AP_SSID);
    ap.ap.channel = 1;
    ap.ap.max_connection = 2;
    ap.ap.authmode = WIFI_AUTH_OPEN;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    ESP_ERROR_CHECK(esp_wifi_start());
    apply_tx_power();
    ESP_LOGW(TAG, "provisioning: join Wi-Fi '%s' and open http://192.168.4.1/", CONFIG_TOY_AP_SSID);

    httpd_config_t hc = HTTPD_DEFAULT_CONFIG();
    hc.stack_size = 8192;
    hc.uri_match_fn = httpd_uri_match_wildcard;
    hc.lru_purge_enable = true;
    httpd_handle_t server = NULL;
    ESP_ERROR_CHECK(httpd_start(&server, &hc));
    httpd_uri_t wifis = { .uri = "/wifis", .method = HTTP_GET, .handler = wifis_get };
    httpd_uri_t save = { .uri = "/save", .method = HTTP_POST, .handler = save_post };
    httpd_uri_t any_get = { .uri = "/*", .method = HTTP_GET, .handler = root_get };
    httpd_uri_t any_post = { .uri = "/*", .method = HTTP_POST, .handler = root_get };
    httpd_register_uri_handler(server, &wifis);
    httpd_register_uri_handler(server, &save);
    httpd_register_uri_handler(server, &any_get);
    httpd_register_uri_handler(server, &any_post);
    xTaskCreate(dns_task, "dns", 3072, NULL, 4, NULL);
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        wifi_sta_list_t stations = { 0 };
        esp_wifi_ap_get_sta_list(&stations);
        ESP_LOGI(TAG, "provisioning AP '%s' is up, %d phone(s) connected, open http://192.168.4.1/",
                 CONFIG_TOY_AP_SSID, stations.num);
    }
}
