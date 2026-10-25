#include "bridge_client.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "ble_adv.h"
#include "player.h"
#include "wifi_prov.h"

static const char *TAG = "bridge";

#define RESP_CAP 8192
#define POLL_PATH "/api/toy/bridge/poll"
#define TOKEN_HEADER "X-Toy-Bridge-Token"

static const toy_config_t *s_cfg;
static char *s_resp;
static int s_resp_len;
static bool s_resp_overflow;
static uint32_t s_seq;
static ble_scan_entry_t s_scan_buf[BLE_SCAN_MAX];
static ble_scan_frame_t s_frames_buf[BLE_SCAN_FRAMES_MAX];

static esp_err_t http_event(esp_http_client_event_t *evt)
{
    if (evt->event_id == HTTP_EVENT_ON_DATA) {
        if (s_resp_len + evt->data_len < RESP_CAP - 1) {
            memcpy(s_resp + s_resp_len, evt->data, evt->data_len);
            s_resp_len += evt->data_len;
            s_resp[s_resp_len] = '\0';
        } else {
            s_resp_overflow = true;
        }
    }
    return ESP_OK;
}

static void hex_bytes(const uint8_t *in, int n, char *out)
{
    static const char *digits = "0123456789abcdef";
    for (int i = 0; i < n; i++) {
        out[i * 2] = digits[in[i] >> 4];
        out[i * 2 + 1] = digits[in[i] & 0xF];
    }
    out[n * 2] = '\0';
}

static bool parse_frame(const char *hex, uint8_t out[BLE_FRAME_LEN])
{
    if (!hex || strlen(hex) != BLE_FRAME_LEN * 2) {
        return false;
    }
    for (int i = 0; i < BLE_FRAME_LEN; i++) {
        char pair[3] = { hex[i * 2], hex[i * 2 + 1], 0 };
        char *end;
        long v = strtol(pair, &end, 16);
        if (*end) {
            return false;
        }
        out[i] = (uint8_t)v;
    }
    return true;
}

static int parse_steps(const cJSON *arr, player_step_t *out, int cap)
{
    int n = 0;
    const cJSON *item;
    cJSON_ArrayForEach(item, arr) {
        if (n >= cap) {
            break;
        }
        const cJSON *frame = cJSON_GetObjectItem(item, "frame");
        const cJSON *ms = cJSON_GetObjectItem(item, "ms");
        if (!cJSON_IsString(frame) || !parse_frame(frame->valuestring, out[n].frame)) {
            return -1;
        }
        double v = cJSON_IsNumber(ms) ? ms->valuedouble : 1000;
        if (v < 20) v = 20;
        if (v > 600000) v = 600000;
        out[n].ms = (uint32_t)v;
        n++;
    }
    return n;
}

static void apply_command(const cJSON *cmd)
{
    player_cmd_t pc;
    memset(&pc, 0, sizeof(pc));
    const cJSON *id = cJSON_GetObjectItem(cmd, "id");
    const cJSON *action = cJSON_GetObjectItem(cmd, "action");
    if (cJSON_IsString(id)) {
        strncpy(pc.id, id->valuestring, PLAYER_ID_MAX - 1);
    }
    const char *act = cJSON_IsString(action) ? action->valuestring : "";

    const cJSON *interval = cJSON_GetObjectItem(cmd, "interval_ms");
    const cJSON *connectable = cJSON_GetObjectItem(cmd, "connectable");
    const cJSON *ad_type = cJSON_GetObjectItem(cmd, "ad_type");
    pc.interval_ms = cJSON_IsNumber(interval) ? (uint16_t)interval->valuedouble : 100;
    pc.connectable = cJSON_IsTrue(connectable);
    pc.ad_type = cJSON_IsNumber(ad_type) ? (uint8_t)ad_type->valuedouble : 0x07;

    const cJSON *tail = cJSON_GetObjectItem(cmd, "tail");
    if (cJSON_IsArray(tail)) {
        int n = parse_steps(tail, pc.tail, PLAYER_MAX_TAIL);
        pc.n_tail = n > 0 ? n : 0;
    }

    if (strcmp(act, "play") == 0) {
        const cJSON *steps = cJSON_GetObjectItem(cmd, "steps");
        const cJSON *loop = cJSON_GetObjectItem(cmd, "loop");
        const cJSON *ttl = cJSON_GetObjectItem(cmd, "ttl_ms");
        int count = cJSON_IsArray(steps) ? cJSON_GetArraySize(steps) : 0;
        if (count <= 0 || count > PLAYER_MAX_STEPS) {
            ESP_LOGW(TAG, "play %s rejected: %d steps", pc.id, count);
            return;
        }
        pc.steps = calloc(count, sizeof(player_step_t));
        if (!pc.steps) {
            return;
        }
        pc.n_steps = parse_steps(steps, pc.steps, count);
        if (pc.n_steps <= 0) {
            free(pc.steps);
            ESP_LOGW(TAG, "play %s rejected: bad frame", pc.id);
            return;
        }
        pc.kind = PLAYER_CMD_PLAY;
        pc.loop = cJSON_IsTrue(loop);
        pc.ttl_ms = cJSON_IsNumber(ttl) ? (uint32_t)ttl->valuedouble : 0;
        if (!player_submit(&pc)) {
            free(pc.steps);
        }
    } else if (strcmp(act, "stop") == 0) {
        pc.kind = PLAYER_CMD_STOP;
        player_submit(&pc);
    } else if (strcmp(act, "scan") == 0) {
        const cJSON *ms = cJSON_GetObjectItem(cmd, "ms");
        const cJSON *active = cJSON_GetObjectItem(cmd, "active");
        pc.kind = PLAYER_CMD_SCAN;
        pc.scan_ms = cJSON_IsNumber(ms) ? (uint32_t)ms->valuedouble : 6000;
        pc.scan_active = !cJSON_IsFalse(active);
        player_submit(&pc);
    } else if (strcmp(act, "reboot") == 0) {
        ESP_LOGW(TAG, "reboot requested");
        vTaskDelay(pdMS_TO_TICKS(300));
        esp_restart();
    }
}

static char *build_body(int *wait_s_out)
{
    cJSON *root = cJSON_CreateObject();
    player_status_t st;
    player_get_status(&st);
    *wait_s_out = strcmp(st.state, "idle") == 0 ? CONFIG_TOY_POLL_WAIT_SECONDS : 4;
    cJSON_AddStringToObject(root, "fw", BRIDGE_FW_VERSION);
    cJSON_AddStringToObject(root, "chip", CONFIG_IDF_TARGET);
    cJSON_AddNumberToObject(root, "seq", ++s_seq);
    cJSON_AddNumberToObject(root, "uptime_s", (double)(esp_timer_get_time() / 1000000));
    cJSON_AddNumberToObject(root, "heap", (double)esp_get_free_heap_size());
    cJSON_AddNumberToObject(root, "rssi", wifi_rssi());
    // 10-09：掉线排查。没有这三个，服务端只能从 uptime 倒退猜「是不是重启了」。
    cJSON_AddNumberToObject(root, "reset", (double)esp_reset_reason());
    cJSON_AddNumberToObject(root, "wifi_drops", wifi_disconnect_count());
    cJSON_AddNumberToObject(root, "wifi_reason", wifi_last_disconnect_reason());
    cJSON_AddBoolToObject(root, "ble_ready", ble_adv_ready());
    cJSON_AddStringToObject(root, "state", st.state);
    if (st.cmd_id[0]) {
        cJSON_AddStringToObject(root, "cmd_id", st.cmd_id);
        cJSON_AddNumberToObject(root, "playing_ms", st.playing_ms);
        cJSON_AddNumberToObject(root, "step", st.step_index);
    }
    if (st.locked_ms) {
        cJSON_AddNumberToObject(root, "locked_ms", st.locked_ms);
    }
    if (st.button) {
        cJSON_AddBoolToObject(root, "button", true);
    }
    cJSON_AddNumberToObject(root, "stops", st.stops);

    player_ack_t ack;
    if (player_take_ack(&ack)) {
        cJSON *a = cJSON_AddObjectToObject(root, "ack");
        cJSON_AddStringToObject(a, "id", ack.id);
        cJSON_AddBoolToObject(a, "ok", ack.ok);
        if (ack.error[0]) {
            cJSON_AddStringToObject(a, "error", ack.error);
        }
    }

    int n = player_take_scan(s_scan_buf, BLE_SCAN_MAX);
    if (n > 0) {
        cJSON *arr = cJSON_AddArrayToObject(root, "scan");
        char hex[64];
        for (int i = 0; i < n; i++) {
            ble_scan_entry_t *e = &s_scan_buf[i];
            cJSON *o = cJSON_CreateObject();
            char addr[18];
            snprintf(addr, sizeof(addr), "%02x:%02x:%02x:%02x:%02x:%02x",
                     e->addr[5], e->addr[4], e->addr[3], e->addr[2], e->addr[1], e->addr[0]);
            cJSON_AddStringToObject(o, "addr", addr);
            cJSON_AddNumberToObject(o, "addr_type", e->addr_type);
            cJSON_AddNumberToObject(o, "rssi", e->rssi);
            cJSON_AddNumberToObject(o, "seen", e->seen);
            if (e->name[0]) {
                cJSON_AddStringToObject(o, "name", e->name);
            }
            if (e->has_uuid) {
                hex_bytes(e->uuid128, 16, hex);
                cJSON_AddStringToObject(o, "uuid128", hex);
            }
            if (e->mfg_len) {
                hex_bytes(e->mfg, e->mfg_len, hex);
                cJSON_AddStringToObject(o, "mfg", hex);
            }
            if (e->raw_len) {
                hex_bytes(e->raw, e->raw_len, hex);
                cJSON_AddStringToObject(o, "raw", hex);
            }
            cJSON_AddItemToArray(arr, o);
        }
    }
    int nf = player_take_scan_frames(s_frames_buf, BLE_SCAN_FRAMES_MAX);
    if (nf > 0) {
        cJSON *arr = cJSON_AddArrayToObject(root, "frames");
        char hex[40];
        for (int i = 0; i < nf; i++) {
            ble_scan_frame_t *f = &s_frames_buf[i];
            cJSON *o = cJSON_CreateObject();
            hex_bytes(f->uuid, 16, hex);
            cJSON_AddStringToObject(o, "uuid128", hex);
            char addr[18];
            snprintf(addr, sizeof(addr), "%02x:%02x:%02x:%02x:%02x:%02x",
                     f->addr[5], f->addr[4], f->addr[3], f->addr[2], f->addr[1], f->addr[0]);
            cJSON_AddStringToObject(o, "addr", addr);
            cJSON_AddNumberToObject(o, "n", f->count);
            cJSON_AddNumberToObject(o, "t0", f->first_ms);
            cJSON_AddNumberToObject(o, "t1", f->last_ms);
            cJSON_AddItemToArray(arr, o);
        }
    }
    char *text = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return text;
}

static void handle_response(void)
{
    cJSON *root = cJSON_Parse(s_resp);
    if (!root) {
        ESP_LOGW(TAG, "bad json (%d bytes%s)", s_resp_len, s_resp_overflow ? ", overflow" : "");
        return;
    }
    const cJSON *watchdog = cJSON_GetObjectItem(root, "watchdog_ms");
    player_touch_server(cJSON_IsNumber(watchdog) ? (uint32_t)watchdog->valuedouble : 0);
    const cJSON *cmd = cJSON_GetObjectItem(root, "cmd");
    if (cJSON_IsObject(cmd)) {
        apply_command(cmd);
    }
    cJSON_Delete(root);
}

static void bridge_task(void *arg)
{
    s_resp = malloc(RESP_CAP);
    if (!s_resp) {
        ESP_LOGE(TAG, "no memory for response buffer");
        vTaskDelete(NULL);
        return;
    }
    char url[TOY_URL_MAX + 64];
    snprintf(url, sizeof(url), "%s" POLL_PATH, s_cfg->url);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .event_handler = http_event,
        .timeout_ms = (CONFIG_TOY_POLL_WAIT_SECONDS + 20) * 1000,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .keep_alive_enable = true,
        // 默认是等 perform() 自己超时（wait+20 秒）才知道连接死了。开 TCP keepalive
        // 探测，断网后 ~20 秒就能发现并重连，而不是干等四十秒。
        .keep_alive_idle = 5,
        .keep_alive_interval = 5,
        .keep_alive_count = 3,
        .buffer_size = 2048,
        .buffer_size_tx = 1024,
        .disable_auto_redirect = true,
    };
    esp_http_client_handle_t client = NULL;
    int failures = 0;

    while (true) {
        if (!wifi_is_connected()) {
            if (!wifi_wait_connected(5000)) {
                ESP_LOGW(TAG, "waiting for Wi-Fi…");
            }
            continue;
        }
        if (!client) {
            client = esp_http_client_init(&cfg);
            if (!client) {
                vTaskDelay(pdMS_TO_TICKS(2000));
                continue;
            }
            esp_http_client_set_header(client, "Content-Type", "application/json");
            esp_http_client_set_header(client, TOKEN_HEADER, s_cfg->token);
        }
        int wait_s = CONFIG_TOY_POLL_WAIT_SECONDS;
        char *body = build_body(&wait_s);
        if (!body) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        char full[sizeof(url) + 24];
        snprintf(full, sizeof(full), "%s?wait=%d", url, wait_s);
        esp_http_client_set_url(client, full);
        esp_http_client_set_post_field(client, body, strlen(body));
        s_resp_len = 0;
        s_resp_overflow = false;
        s_resp[0] = '\0';
        esp_err_t err = esp_http_client_perform(client);
        int status = esp_http_client_get_status_code(client);
        free(body);

        if (err == ESP_OK && status == 200) {
            failures = 0;
            if (s_seq % 10 == 1) {
                ESP_LOGI(TAG, "server ok (poll #%u, heap %u)", (unsigned)s_seq, (unsigned)esp_get_free_heap_size());
            }
            handle_response();
            continue;
        }
        failures++;
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "poll failed: %s (fail %d)", esp_err_to_name(err), failures);
            esp_http_client_close(client);
        } else {
            ESP_LOGW(TAG, "poll http %d (fail %d): %.120s", status, failures, s_resp);
        }
        if (failures >= 8) {
            esp_http_client_cleanup(client);
            client = NULL;
            failures = 0;
        }
        vTaskDelay(pdMS_TO_TICKS(status == 401 || status == 403 ? 15000 : (failures < 3 ? 2000 : 8000)));
    }
}

void bridge_client_start(const toy_config_t *cfg)
{
    s_cfg = cfg;
    xTaskCreate(bridge_task, "bridge", 10240, NULL, 5, NULL);
}
