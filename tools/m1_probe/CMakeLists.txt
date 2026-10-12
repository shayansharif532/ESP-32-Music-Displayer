# -DM1_PROBE=0  -> empty component, i.e. the pristine Waveshare demo with no sidecar.
if(DEFINED M1_PROBE AND NOT M1_PROBE)
    idf_component_register()
    return()
endif()

idf_component_register(
    SRCS "m1_probe.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES heap waveshare__esp32_s3_touch_lcd_4b lvgl__lvgl
    WHOLE_ARCHIVE)   # nothing references this component, so its constructor must be force-linked
