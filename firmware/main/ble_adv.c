#include "ble_adv.h"

#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "host/ble_gap.h"
#include "host/ble_hs.h"
#include "host/ble_hs_adv.h"
#include "host/util/util.h"
#include "nimble/hci_common.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"

static const char *TAG = "ble";

static volatile bool s_synced;
static uint8_t s_addr_type;
static bool s_adv_wanted;
static uint8_t s_adv_data[31];
static int s_adv_len;
static struct ble_gap_adv_params s_adv_params;

static SemaphoreHandle_t s_scan_done;
static SemaphoreHandle_t s_scan_lock;
static ble_scan_entry_t s_scan[BLE_SCAN_MAX];
static int s_scan_count;
static ble_scan_frame_t s_frames[BLE_SCAN_FRAMES_MAX];
static int s_frames_count;
static int64_t s_scan_started_us;

static int gap_event(struct ble_gap_event *event, void *arg);

static void on_sync(void)
{
    int rc = ble_hs_util_ensure_addr(0);
    if (rc != 0) {
        ESP_LOGE(TAG, "ensure_addr rc=%d", rc);
    }
    rc = ble_hs_id_infer_auto(0, &s_addr_type);
    if (rc != 0) {
        ESP_LOGE(TAG, "infer_auto rc=%d", rc);
    }
    s_synced = true;
    ESP_LOGI(TAG, "host synced, addr_type=%d", s_addr_type);
}

static void on_reset(int reason)
{
    s_synced = false;
    ESP_LOGW(TAG, "host reset reason=%d", reason);
}

static void host_task(void *param)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

void ble_adv_init(void)
{
    s_scan_done = xSemaphoreCreateBinary();
    s_scan_lock = xSemaphoreCreateMutex();
    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    ble_svc_gap_init();
    ble_svc_gap_device_name_set("shikong-bridge");
    nimble_port_freertos_init(host_task);
    for (int i = 0; i < 100 && !s_synced; i++) {
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    if (!s_synced) {
        ESP_LOGE(TAG, "host did not sync within 5 s");
    }
}

bool ble_adv_ready(void)
{
    return s_synced;
}

bool ble_adv_active(void)
{
    return ble_gap_adv_active() != 0;
}

static int start_adv(void)
{
    int rc = ble_gap_adv_set_data(s_adv_data, s_adv_len);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv_set_data rc=%d", rc);
        return rc;
    }
    rc = ble_gap_adv_start(s_addr_type, NULL, BLE_HS_FOREVER, &s_adv_params, gap_event, NULL);
    if (rc != 0 && rc != BLE_HS_EALREADY) {
        ESP_LOGE(TAG, "adv_start rc=%d", rc);
    }
    return rc == BLE_HS_EALREADY ? 0 : rc;
}

int ble_adv_set_frame(const uint8_t frame[BLE_FRAME_LEN], uint16_t interval_ms, bool connectable, uint8_t ad_type)
{
    if (!s_synced) {
        return -1;
    }
    int n = 0;
    s_adv_data[n++] = 2;
    s_adv_data[n++] = 0x01;          // Flags
    s_adv_data[n++] = 0x06;          // LE General Discoverable, BR/EDR not supported
    s_adv_data[n++] = 17;
    s_adv_data[n++] = ad_type ? ad_type : 0x07;   // complete list of 128-bit service UUIDs
    memcpy(&s_adv_data[n], frame, BLE_FRAME_LEN);
    n += BLE_FRAME_LEN;
    s_adv_len = n;

    uint32_t units = ((uint32_t)interval_ms * 1000u) / 625u;
    if (units < 32) {
        units = 32;                                // 20 ms is the spec minimum
    }
    if (!connectable && units < 160) {
        units = 160;                               // legacy non-connectable minimum is 100 ms
    }
    if (units > 16384) {
        units = 16384;
    }
    memset(&s_adv_params, 0, sizeof(s_adv_params));
    s_adv_params.conn_mode = connectable ? BLE_GAP_CONN_MODE_UND : BLE_GAP_CONN_MODE_NON;
    s_adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    s_adv_params.itvl_min = units;
    s_adv_params.itvl_max = units;

    if (ble_gap_adv_active()) {
        ble_gap_adv_stop();
    }
    s_adv_wanted = true;
    return start_adv();
}

void ble_adv_stop(void)
{
    s_adv_wanted = false;
    if (ble_gap_adv_active()) {
        ble_gap_adv_stop();
    }
}

static int gap_event(struct ble_gap_event *event, void *arg)
{
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        // We are a broadcaster; drop anyone who connects and keep advertising.
        ESP_LOGW(TAG, "unexpected central connected, terminating");
        if (event->connect.status == 0) {
            ble_gap_terminate(event->connect.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
        } else if (s_adv_wanted) {
            start_adv();
        }
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
    case BLE_GAP_EVENT_ADV_COMPLETE:
        if (s_adv_wanted) {
            start_adv();
        }
        return 0;
    default:
        return 0;
    }
}

// ---------------------------------------------------------------- scanning

static ble_scan_entry_t *find_or_add(const ble_addr_t *addr)
{
    for (int i = 0; i < s_scan_count; i++) {
        if (s_scan[i].addr_type == addr->type && memcmp(s_scan[i].addr, addr->val, 6) == 0) {
            return &s_scan[i];
        }
    }
    if (s_scan_count >= BLE_SCAN_MAX) {
        return NULL;
    }
    ble_scan_entry_t *e = &s_scan[s_scan_count++];
    memset(e, 0, sizeof(*e));
    memcpy(e->addr, addr->val, 6);
    e->addr_type = addr->type;
    return e;
}

static int scan_event(struct ble_gap_event *event, void *arg)
{
    if (event->type == BLE_GAP_EVENT_DISC_COMPLETE) {
        xSemaphoreGive(s_scan_done);
        return 0;
    }
    if (event->type != BLE_GAP_EVENT_DISC) {
        return 0;
    }
    const struct ble_gap_disc_desc *d = &event->disc;
    if (xSemaphoreTake(s_scan_lock, 0) != pdTRUE) {
        return 0;
    }
    ble_scan_entry_t *e = find_or_add(&d->addr);
    if (e) {
        e->rssi = d->rssi;
        e->seen++;
        bool is_rsp = d->event_type == BLE_HCI_ADV_RPT_EVTYPE_SCAN_RSP;
        if (!is_rsp && d->length_data <= sizeof(e->raw)) {
            e->raw_len = d->length_data;
            memcpy(e->raw, d->data, d->length_data);
        }
        struct ble_hs_adv_fields f;
        if (ble_hs_adv_parse_fields(&f, d->data, d->length_data) == 0) {
            if (f.name && f.name_len) {
                int n = f.name_len < sizeof(e->name) - 1 ? f.name_len : sizeof(e->name) - 1;
                memcpy(e->name, f.name, n);
                e->name[n] = '\0';
            }
            if (f.num_uuids128 > 0) {
                memcpy(e->uuid128, f.uuids128[0].value, 16);
                e->has_uuid = true;
                uint32_t t = (uint32_t)((esp_timer_get_time() - s_scan_started_us) / 1000);
                ble_scan_frame_t *fr = NULL;
                for (int i = 0; i < s_frames_count; i++) {
                    if (memcmp(s_frames[i].uuid, f.uuids128[0].value, 16) == 0) {
                        fr = &s_frames[i];
                        break;
                    }
                }
                if (!fr && s_frames_count < BLE_SCAN_FRAMES_MAX) {
                    fr = &s_frames[s_frames_count++];
                    memset(fr, 0, sizeof(*fr));
                    memcpy(fr->uuid, f.uuids128[0].value, 16);
                    memcpy(fr->addr, d->addr.val, 6);
                    fr->first_ms = t;
                }
                if (fr) {
                    fr->count++;
                    fr->last_ms = t;
                }
            }
            if (f.mfg_data && f.mfg_data_len) {
                int n = f.mfg_data_len < sizeof(e->mfg) ? f.mfg_data_len : sizeof(e->mfg);
                memcpy(e->mfg, f.mfg_data, n);
                e->mfg_len = n;
            }
        }
    }
    xSemaphoreGive(s_scan_lock);
    return 0;
}

int ble_scan_blocking(uint32_t ms, bool active, ble_scan_entry_t *out, int cap)
{
    if (!s_synced) {
        return -1;
    }
    if (ms < 500) {
        ms = 500;
    }
    if (ms > 30000) {
        ms = 30000;
    }
    xSemaphoreTake(s_scan_lock, portMAX_DELAY);
    s_scan_count = 0;
    s_frames_count = 0;
    s_scan_started_us = esp_timer_get_time();
    xSemaphoreGive(s_scan_lock);
    xSemaphoreTake(s_scan_done, 0);

    struct ble_gap_disc_params dp = {
        .itvl = 0x60,
        .window = 0x50,
        .filter_policy = 0,
        .limited = 0,
        .passive = active ? 0 : 1,
        .filter_duplicates = 0,
    };
    int rc = ble_gap_disc(s_addr_type, ms, &dp, scan_event, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "scan start rc=%d", rc);
        return -1;
    }
    if (xSemaphoreTake(s_scan_done, pdMS_TO_TICKS(ms + 2000)) != pdTRUE) {
        ble_gap_disc_cancel();
    }
    xSemaphoreTake(s_scan_lock, portMAX_DELAY);
    int n = s_scan_count < cap ? s_scan_count : cap;
    memcpy(out, s_scan, n * sizeof(ble_scan_entry_t));
    xSemaphoreGive(s_scan_lock);
    return n;
}

int ble_scan_take_frames(ble_scan_frame_t *out, int cap)
{
    xSemaphoreTake(s_scan_lock, portMAX_DELAY);
    int n = s_frames_count < cap ? s_frames_count : cap;
    if (n > 0) {
        memcpy(out, s_frames, n * sizeof(ble_scan_frame_t));
    }
    xSemaphoreGive(s_scan_lock);
    return n;
}
