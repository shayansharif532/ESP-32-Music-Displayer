# The following lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.5)

# The firmware version (shown in the USB BEACON and the companion's About box); keep in step with docs/updating.md.
set(PROJECT_VER "1.0.0")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

add_compile_options(-Wno-format)

# Development-only helpers (screenshot dump, simulated taps): idf.py -DAPP_DEV_TOOLS=1 build. Never enabled for a product build.
if(APP_DEV_TOOLS)
    add_compile_definitions(APP_DEV_TOOLS=1)
endif()

project(deskmedia_display)
