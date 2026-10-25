#pragma once
#include <stdbool.h>
#include <stdint.h>

#define BLE_FRAME_LEN 16
#define BLE_SCAN_MAX 24

typedef struct {
    uint8_t addr[6];
    uint8_t addr_type;
    int8_t rssi;
    uint16_t seen;
    char name[24];
    bool has_uuid;
    uint8_t uuid128[16];      // on-air (little-endian) byte order
    uint8_t mfg_len;
    uint8_t mfg[24];
    uint8_t raw_len;
    uint8_t raw[31];          // last advertising PDU payload (not scan response)
} ble_scan_entry_t;

#define BLE_SCAN_FRAMES_MAX 48
typedef struct {
    uint8_t uuid[16];        // on-air order
    uint8_t addr[6];
    uint16_t count;
    uint32_t first_ms;       // offset from scan start
    uint32_t last_ms;
} ble_scan_frame_t;

void ble_adv_init(void);
bool ble_adv_ready(void);
// Advertise one 16-byte frame as a 128-bit service UUID, bytes in on-air order.
int  ble_adv_set_frame(const uint8_t frame[BLE_FRAME_LEN], uint16_t interval_ms, bool connectable, uint8_t ad_type);
void ble_adv_stop(void);
bool ble_adv_active(void);
// Blocking scan; returns number of entries written to out (<= cap), or -1.
int  ble_scan_blocking(uint32_t ms, bool active, ble_scan_entry_t *out, int cap);
// Distinct 128-bit UUIDs seen during the last scan, in order of first sighting.
int  ble_scan_take_frames(ble_scan_frame_t *out, int cap);
