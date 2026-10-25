#include "config_store.h"

#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

static const char *TAG = "config";
#define NS "toy"

void config_store_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
}

static void get_str(nvs_handle_t h, const char *key, char *dst, size_t cap, const char *fallback)
{
    size_t len = cap;
    if (nvs_get_str(h, key, dst, &len) != ESP_OK) {
        strncpy(dst, fallback, cap - 1);
        dst[cap - 1] = '\0';
    }
}

bool config_store_load(toy_config_t *out)
{
    memset(out, 0, sizeof(*out));
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) {
        strncpy(out->url, CONFIG_TOY_DEFAULT_URL, TOY_URL_MAX - 1);
        return false;
    }
    get_str(h, "ssid", out->ssid, TOY_SSID_MAX, "");
    get_str(h, "pass", out->pass, TOY_PASS_MAX, "");
    get_str(h, "url", out->url, TOY_URL_MAX, CONFIG_TOY_DEFAULT_URL);
    get_str(h, "token", out->token, TOY_TOKEN_MAX, "");
    nvs_close(h);
    return config_store_is_complete(out);
}

bool config_store_save(const toy_config_t *cfg)
{
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "nvs open failed");
        return false;
    }
    bool ok = nvs_set_str(h, "ssid", cfg->ssid) == ESP_OK
        && nvs_set_str(h, "pass", cfg->pass) == ESP_OK
        && nvs_set_str(h, "url", cfg->url) == ESP_OK
        && nvs_set_str(h, "token", cfg->token) == ESP_OK
        && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}

void config_store_clear(void)
{
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_erase_all(h);
        nvs_commit(h);
        nvs_close(h);
    }
}

bool config_store_is_complete(const toy_config_t *cfg)
{
    return cfg->ssid[0] && cfg->url[0] && cfg->token[0];
}
