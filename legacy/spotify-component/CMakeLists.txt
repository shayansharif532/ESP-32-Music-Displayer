idf_component_register(
    SRCS "spotify_http.c" "spotify_auth.c" "spotify_api.c" "spotify_art.c" "spotify_setup.c"
    INCLUDE_DIRS "include"
    REQUIRES lvgl__lvgl esp_http_client
    PRIV_REQUIRES esp-tls esp_http_server json mbedtls nvs_flash esp_timer esp_system heap log freertos
                  wifi_mgr espressif__esp_jpeg)
