idf_component_register(
    SRCS "side_keys.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_common
    PRIV_REQUIRES driver esp_timer waveshare__esp32_s3_touch_lcd_4b)
