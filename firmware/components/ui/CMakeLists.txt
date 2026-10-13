idf_component_register(
    SRCS "ui.c" "ui_pairing.c"
    INCLUDE_DIRS "include"
    REQUIRES lvgl__lvgl
    PRIV_REQUIRES waveshare__esp32_s3_touch_lcd_4b heap)
