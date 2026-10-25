#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "config_store.h"

void wifi_sta_start(const toy_config_t *cfg);
bool wifi_wait_connected(uint32_t ms);
bool wifi_is_connected(void);
int  wifi_rssi(void);
int  wifi_disconnect_count(void);
int  wifi_last_disconnect_reason(void);
// Open a provisioning access point with a web form; reboots after saving. Never returns.
void wifi_provisioning_run(toy_config_t *cfg);
