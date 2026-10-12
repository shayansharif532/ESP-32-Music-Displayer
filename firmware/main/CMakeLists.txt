idf_component_register(
    SRCS "main.c" "health.c" "demo_player.c" "net_selftest.c"
    INCLUDE_DIRS "." ".."                 # ".." = firmware/, where the git-ignored secrets.local.h lives
    REQUIRES ui wifi_mgr spotify
    PRIV_REQUIRES nvs_flash esp_timer esp_http_client esp-tls mbedtls heap lvgl__lvgl waveshare__esp32_s3_touch_lcd_4b)

# Dev self-tests, off by default:  idf.py -DAPP_WIFI_SELFTEST=1 build   (drops the Wi-Fi link once and times the recovery)
if(APP_WIFI_SELFTEST)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE APP_WIFI_SELFTEST=1)
endif()
