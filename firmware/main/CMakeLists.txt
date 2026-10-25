idf_component_register(
    SRCS "main.c" "config_store.c" "wifi_prov.c" "ble_adv.c" "player.c" "bridge_client.c" "button.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_wifi esp_netif esp_event esp_http_client esp_http_server
             esp-tls mbedtls bt json esp_driver_gpio esp_timer esp_system
)
