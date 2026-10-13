idf_component_register(
    SRCS "dmlink.c" "usb_link.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_driver_usb_serial_jtag
    PRIV_REQUIRES json esp_timer heap log freertos esp_ringbuf esp_hw_support)
