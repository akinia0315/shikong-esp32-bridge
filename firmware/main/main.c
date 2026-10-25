#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "ble_adv.h"
#include "bridge_client.h"
#include "button.h"
#include "config_store.h"
#include "player.h"
#include "wifi_prov.h"

static const char *TAG = "main";
static toy_config_t s_cfg;

void app_main(void)
{
    config_store_init();
    button_init();
    bool complete = config_store_load(&s_cfg);
    ESP_LOGI(TAG, "shikong bridge fw %s, config %s", BRIDGE_FW_VERSION, complete ? "present" : "missing");
    if (!complete) {
        wifi_provisioning_run(&s_cfg);     // never returns
    }
    ble_adv_init();
    player_init();
    wifi_sta_start(&s_cfg);
    bridge_client_start(&s_cfg);
}
