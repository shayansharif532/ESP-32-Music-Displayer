set(srcs "main.c" "health.c" "demo_player.c")
if(APP_DEV_TOOLS)
    list(APPEND srcs "dev_tools.c")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "." ".."                 # ".." = firmware/, where the git-ignored secrets.local.h lives
    REQUIRES ui wifi_mgr pc_media side_keys
    PRIV_REQUIRES nvs_flash esp_timer heap lvgl__lvgl waveshare__esp32_s3_touch_lcd_4b mbedtls esp_rom)

# Dev self-tests, off by default:  idf.py -DAPP_WIFI_SELFTEST=1 build   (drops the Wi-Fi link once and times the recovery)
if(APP_WIFI_SELFTEST)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE APP_WIFI_SELFTEST=1)
endif()
