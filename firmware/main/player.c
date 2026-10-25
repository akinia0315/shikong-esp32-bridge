#include "player.h"

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "button.h"

static const char *TAG = "player";

#define EMERGENCY_LOCK_MS 30000
#define DEFAULT_WATCHDOG_MS 15000
#define TAIL_STEP_MS 250

static QueueHandle_t s_queue;
static SemaphoreHandle_t s_lock;

static player_cmd_t s_cur;
static bool s_playing;
static int s_idx;
static int64_t s_step_started;
static int64_t s_play_started;
static int64_t s_last_touch;
static uint32_t s_watchdog_ms = DEFAULT_WATCHDOG_MS;
static int64_t s_locked_until;
static bool s_button_flag;
static uint32_t s_stops;
static bool s_scanning;

static player_step_t s_known_stop[PLAYER_MAX_TAIL];
static int s_known_stop_n;

static player_ack_t s_ack;
static ble_scan_entry_t s_scan_out[BLE_SCAN_MAX];
static int s_scan_n;
static ble_scan_frame_t s_frames_out[BLE_SCAN_FRAMES_MAX];
static int s_frames_n;

static inline int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

// README frame layout: [cnt] 02 [lvl] 00 00 64 00 01 27 C6 00 [mode] [sum] 17 00 71
static void builtin_stop_frame(uint8_t out[BLE_FRAME_LEN])
{
    uint8_t cnt = (uint8_t)esp_random();
    const uint8_t base[BLE_FRAME_LEN] = {
        0x00, 0x02, 0x00, 0x00, 0x00, 0x64, 0x00, 0x01,
        0x27, 0xC6, 0x00, 0x01, 0x00, 0x17, 0x00, 0x71,
    };
    memcpy(out, base, BLE_FRAME_LEN);
    out[0] = cnt;
    out[12] = (uint8_t)((cnt + 0x00 + 0x01 + 0x22) & 0xFF);
}

static void set_ack(const char *id, bool ok, const char *error)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_ack.pending = true;
    strncpy(s_ack.id, id ? id : "", PLAYER_ID_MAX - 1);
    s_ack.id[PLAYER_ID_MAX - 1] = '\0';
    s_ack.ok = ok;
    strncpy(s_ack.error, error ? error : "", sizeof(s_ack.error) - 1);
    s_ack.error[sizeof(s_ack.error) - 1] = '\0';
    xSemaphoreGive(s_lock);
}

static void free_current(void)
{
    free(s_cur.steps);
    s_cur.steps = NULL;
    s_cur.n_steps = 0;
}

static void run_stop(const player_step_t *tail, int n_tail, const char *why)
{
    ESP_LOGI(TAG, "stop (%s)", why);
    uint16_t interval = s_cur.interval_ms ? s_cur.interval_ms : 100;
    bool connectable = s_cur.connectable;
    uint8_t ad_type = s_cur.ad_type;
    if (n_tail <= 0) {
        tail = s_known_stop;
        n_tail = s_known_stop_n;
    }
    player_step_t fallback;
    if (n_tail <= 0) {
        builtin_stop_frame(fallback.frame);
        fallback.ms = 600;
        tail = &fallback;
        n_tail = 1;
    }
    for (int i = 0; i < n_tail; i++) {
        if (ble_adv_set_frame(tail[i].frame, interval, connectable, ad_type) == 0) {
            vTaskDelay(pdMS_TO_TICKS(tail[i].ms ? tail[i].ms : TAIL_STEP_MS));
        }
    }
    ble_adv_stop();
    s_playing = false;
    s_stops++;
    free_current();
}

static void start_play(const player_cmd_t *cmd)
{
    if (s_playing) {
        free_current();
        s_playing = false;
    }
    s_cur = *cmd;                        // takes ownership of cmd->steps
    if (cmd->n_tail > 0) {
        memcpy(s_known_stop, cmd->tail, sizeof(player_step_t) * cmd->n_tail);
        s_known_stop_n = cmd->n_tail;
    }
    s_idx = 0;
    s_play_started = now_ms();
    s_step_started = s_play_started;
    int rc = ble_adv_set_frame(s_cur.steps[0].frame, s_cur.interval_ms, s_cur.connectable, s_cur.ad_type);
    if (rc != 0) {
        set_ack(cmd->id, false, "ble advertise failed");
        free_current();
        return;
    }
    s_playing = true;
    set_ack(cmd->id, true, "");
    ESP_LOGI(TAG, "play %s: %d steps loop=%d ttl=%u", cmd->id, cmd->n_steps, cmd->loop, (unsigned)cmd->ttl_ms);
}

static void handle_cmd(player_cmd_t *cmd)
{
    int64_t now = now_ms();
    switch (cmd->kind) {
    case PLAYER_CMD_PLAY:
        if (now < s_locked_until) {
            set_ack(cmd->id, false, "emergency lock active");
            free(cmd->steps);
            return;
        }
        if (cmd->n_steps <= 0 || !cmd->steps) {
            set_ack(cmd->id, false, "no steps");
            free(cmd->steps);
            return;
        }
        if (!ble_adv_ready()) {
            set_ack(cmd->id, false, "ble not ready");
            free(cmd->steps);
            return;
        }
        start_play(cmd);
        return;
    case PLAYER_CMD_STOP:
        if (cmd->n_tail > 0) {
            memcpy(s_known_stop, cmd->tail, sizeof(player_step_t) * cmd->n_tail);
            s_known_stop_n = cmd->n_tail;
        }
        run_stop(cmd->tail, cmd->n_tail, "command");
        set_ack(cmd->id, true, "");
        return;
    case PLAYER_CMD_SCAN:
        if (s_playing) {
            set_ack(cmd->id, false, "busy playing");
            return;
        }
        s_scanning = true;
        int n = ble_scan_blocking(cmd->scan_ms, cmd->scan_active, s_scan_out, BLE_SCAN_MAX);
        s_scanning = false;
        if (n < 0) {
            set_ack(cmd->id, false, "scan failed");
            return;
        }
        xSemaphoreTake(s_lock, portMAX_DELAY);
        s_scan_n = n;
        s_frames_n = ble_scan_take_frames(s_frames_out, BLE_SCAN_FRAMES_MAX);
        xSemaphoreGive(s_lock);
        set_ack(cmd->id, true, "");
        return;
    }
}

static void advance(void)
{
    int64_t now = now_ms();
    if (s_cur.ttl_ms && now - s_play_started >= (int64_t)s_cur.ttl_ms) {
        run_stop(s_cur.tail, s_cur.n_tail, "ttl");
        return;
    }
    if (now - s_step_started < (int64_t)s_cur.steps[s_idx].ms) {
        return;
    }
    s_idx++;
    if (s_idx >= s_cur.n_steps) {
        if (!s_cur.loop) {
            run_stop(s_cur.tail, s_cur.n_tail, "end of steps");
            return;
        }
        s_idx = 0;
    }
    s_step_started = now;
    ble_adv_set_frame(s_cur.steps[s_idx].frame, s_cur.interval_ms, s_cur.connectable, s_cur.ad_type);
}

static void player_task(void *arg)
{
    player_cmd_t cmd;
    while (true) {
        if (xQueueReceive(s_queue, &cmd, pdMS_TO_TICKS(25)) == pdTRUE) {
            handle_cmd(&cmd);
        }
        if (button_take_emergency()) {
            s_locked_until = now_ms() + EMERGENCY_LOCK_MS;
            s_button_flag = true;
            if (s_playing) {
                run_stop(NULL, 0, "button");
            } else {
                run_stop(NULL, 0, "button (idle, resend stop)");
            }
        }
        if (s_playing) {
            if (now_ms() - s_last_touch > (int64_t)s_watchdog_ms) {
                run_stop(s_cur.tail, s_cur.n_tail, "server watchdog");
            } else {
                advance();
            }
        }
    }
}

void player_init(void)
{
    s_queue = xQueueCreate(4, sizeof(player_cmd_t));
    s_lock = xSemaphoreCreateMutex();
    s_last_touch = now_ms();
    xTaskCreate(player_task, "player", 4096, NULL, 6, NULL);
}

bool player_submit(const player_cmd_t *cmd)
{
    if (xQueueSend(s_queue, cmd, pdMS_TO_TICKS(100)) != pdTRUE) {
        ESP_LOGW(TAG, "queue full, dropping %s", cmd->id);
        return false;
    }
    return true;
}

void player_touch_server(uint32_t watchdog_ms)
{
    s_last_touch = now_ms();
    if (watchdog_ms >= 3000 && watchdog_ms <= 120000) {
        s_watchdog_ms = watchdog_ms;
    }
}

void player_get_status(player_status_t *out)
{
    int64_t now = now_ms();
    memset(out, 0, sizeof(*out));
    out->state = s_scanning ? "scanning" : (s_playing ? "playing" : "idle");
    if (s_playing) {
        strncpy(out->cmd_id, s_cur.id, PLAYER_ID_MAX - 1);
        out->playing_ms = (uint32_t)(now - s_play_started);
        out->step_index = s_idx;
    }
    out->locked_ms = now < s_locked_until ? (uint32_t)(s_locked_until - now) : 0;
    out->button = s_button_flag;
    s_button_flag = false;
    out->stops = s_stops;
}

bool player_take_ack(player_ack_t *out)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    bool had = s_ack.pending;
    if (had) {
        *out = s_ack;
        s_ack.pending = false;
    }
    xSemaphoreGive(s_lock);
    return had;
}

int player_take_scan(ble_scan_entry_t *out, int cap)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    int n = s_scan_n < cap ? s_scan_n : cap;
    if (n > 0) {
        memcpy(out, s_scan_out, n * sizeof(ble_scan_entry_t));
    }
    s_scan_n = 0;
    xSemaphoreGive(s_lock);
    return n;
}

int player_take_scan_frames(ble_scan_frame_t *out, int cap)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    int n = s_frames_n < cap ? s_frames_n : cap;
    if (n > 0) {
        memcpy(out, s_frames_out, n * sizeof(ble_scan_frame_t));
    }
    s_frames_n = 0;
    xSemaphoreGive(s_lock);
    return n;
}
