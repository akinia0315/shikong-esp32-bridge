#include "button.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "config_store.h"

static const char *TAG = "button";
static volatile bool s_emergency;

static void button_task(void *arg)
{
    const gpio_num_t pin = CONFIG_TOY_BUTTON_GPIO;
    int64_t pressed_at = 0;
    bool was_down = false;
    while (true) {
        bool down = gpio_get_level(pin) == 0;
        int64_t now = esp_timer_get_time();
        if (down && !was_down) {
            pressed_at = now;
        } else if (down && was_down && now - pressed_at >= 6000000) {
            ESP_LOGW(TAG, "long press: wiping config, rebooting into provisioning");
            config_store_clear();
            vTaskDelay(pdMS_TO_TICKS(200));
            esp_restart();
        } else if (!down && was_down) {
            if (now - pressed_at >= 30000) {   // debounce 30 ms
                ESP_LOGW(TAG, "emergency stop pressed");
                s_emergency = true;
            }
        }
        was_down = down;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void button_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << CONFIG_TOY_BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    xTaskCreate(button_task, "button", 2048, NULL, 5, NULL);
}

bool button_take_emergency(void)
{
    if (!s_emergency) {
        return false;
    }
    s_emergency = false;
    return true;
}
