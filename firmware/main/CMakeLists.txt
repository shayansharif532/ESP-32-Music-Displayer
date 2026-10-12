idf_component_register(
    SRCS "main.c" "health.c"
    INCLUDE_DIRS "."
    REQUIRES ui
    PRIV_REQUIRES nvs_flash esp_timer heap lvgl__lvgl waveshare__esp32_s3_touch_lcd_4b)
