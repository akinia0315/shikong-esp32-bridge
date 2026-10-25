#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "ble_adv.h"

#define PLAYER_MAX_STEPS 100
#define PLAYER_MAX_TAIL 4
#define PLAYER_ID_MAX 32

typedef struct {
    uint8_t frame[BLE_FRAME_LEN];
    uint32_t ms;
} player_step_t;

typedef enum { PLAYER_CMD_PLAY = 0, PLAYER_CMD_STOP, PLAYER_CMD_SCAN } player_cmd_kind_t;

typedef struct {
    player_cmd_kind_t kind;
    char id[PLAYER_ID_MAX];
    player_step_t *steps;        // heap; ownership passes to the player on submit
    int n_steps;
    player_step_t tail[PLAYER_MAX_TAIL];
    int n_tail;
    bool loop;
    uint32_t ttl_ms;
    uint16_t interval_ms;
    bool connectable;
    uint8_t ad_type;
    uint32_t scan_ms;
    bool scan_active;
} player_cmd_t;

typedef struct {
    bool pending;
    char id[PLAYER_ID_MAX];
    bool ok;
    char error[48];
} player_ack_t;

typedef struct {
    const char *state;           // "idle" | "playing" | "scanning"
    char cmd_id[PLAYER_ID_MAX];
    uint32_t playing_ms;
    int step_index;
    uint32_t locked_ms;          // remaining emergency lock, 0 if none
    bool button;                 // emergency pressed since last report
    uint32_t stops;              // total stops executed (watchdog + button + commands)
} player_status_t;

void player_init(void);
bool player_submit(const player_cmd_t *cmd);
void player_touch_server(uint32_t watchdog_ms);
void player_get_status(player_status_t *out);
bool player_take_ack(player_ack_t *out);
int  player_take_scan(ble_scan_entry_t *out, int cap);
int  player_take_scan_frames(ble_scan_frame_t *out, int cap);
