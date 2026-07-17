# 初始化源文件和包含目录
set(SOURCES
    "./main.c"
    "./hardeware_driver/bsp_board.c"
    "./speech_det_driver/mic_speech.c"
    "./tca9555_driver/tca9555_driver.c"
    "./led_ring_driver/led_ring.c"
    "./audio_echo_driver/audio_echo.c"
    "./wifi_driver/wifi_conn.c"
    "./voice_assistant_driver/voice_assistant.c"
    "./app_config_driver/app_config.c"
    "./config_portal_driver/config_portal.c"
)

set(INCLUDE_DIRS
    "."
    "./hardeware_driver"
    "./speech_det_driver"
    "./tca9555_driver"
    "./led_ring_driver"
    "./audio_echo_driver"
    "./wifi_driver"
    "./voice_assistant_driver"
    "./app_config_driver"
    "./config_portal_driver"
)


# 注册组件
idf_component_register(
    SRCS ${SOURCES}
    INCLUDE_DIRS ${INCLUDE_DIRS}
)
