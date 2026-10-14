idf_component_register(
    SRCS "idle_screen.c" "idle_date.c" "font_clock.c"
    INCLUDE_DIRS "include"
    REQUIRES lvgl__lvgl
    PRIV_REQUIRES waveshare__esp32_s3_touch_lcd_4b heap json nvs_flash esp_partition esp_timer esp_rom log freertos espressif__esp_jpeg)
