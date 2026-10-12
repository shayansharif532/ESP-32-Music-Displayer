idf_component_register(
    SRCS "pc_media.c" "pc_http.c" "pc_art.c"
    INCLUDE_DIRS "include"
    REQUIRES lvgl__lvgl esp_http_client
    PRIV_REQUIRES json lwip heap log freertos esp_timer wifi_mgr espressif__esp_jpeg)
