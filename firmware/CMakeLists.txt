# The following lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.5)

# The firmware version (shown in the USB BEACON and the companion's About box); keep in step with docs/updating.md.
set(PROJECT_VER "1.0.0")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

add_compile_options(-Wno-format)

# cJSON's default nesting limit is 1000 levels, about 64 bytes of stack each: a peer that sends "[[[[..." would overflow the 4 KB / 12 KB task stacks
# that parse what arrives over the link. Every JSON this firmware parses is at most three levels deep.
add_compile_definitions(CJSON_NESTING_LIMIT=8)

# Development-only helpers (screenshot dump, simulated taps): idf.py -DAPP_DEV_TOOLS=1 build. Never enabled for a product build.
if(APP_DEV_TOOLS)
    add_compile_definitions(APP_DEV_TOOLS=1)
endif()

project(deskmedia_display)
