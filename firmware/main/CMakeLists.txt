set(srcs "main.c" "health.c")
if(APP_UI_DEMO)
    list(APPEND srcs "demo_player.c")
endif()
if(APP_DEV_TOOLS)
    list(APPEND srcs "dev_tools.c")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "." ".."                 # ".." = firmware/, where the git-ignored secrets.local.h lives
    REQUIRES ui idle_screen wifi_mgr pc_media side_keys usb_link
    PRIV_REQUIRES json nvs_flash esp_timer heap lvgl__lvgl waveshare__esp32_s3_touch_lcd_4b mbedtls esp_rom esp_app_format)

# Development builds only:  idf.py -DAPP_UI_DEMO=1 build   = a scripted fake player instead of the PC link (UI work without a PC)
if(APP_UI_DEMO)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE APP_UI_DEMO=1)
endif()

# The address the "install DeskMedia Companion" screen prints (see app_config.h):
#   idf.py -DDM_SETUP_ADDRESS=example.org/deskmedia build
if(DM_SETUP_ADDRESS)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE DM_SETUP_ADDRESS="${DM_SETUP_ADDRESS}")
endif()

# Dev self-tests, off by default:  idf.py -DAPP_WIFI_SELFTEST=1 build   (drops the Wi-Fi link once and times the recovery)
if(APP_WIFI_SELFTEST)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE APP_WIFI_SELFTEST=1)
endif()

# Build the product configuration even when firmware/secrets.local.h exists on this machine:  idf.py -DAPP_NO_WIFI=1 build
if(APP_NO_WIFI)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE APP_NO_WIFI=1)
endif()
