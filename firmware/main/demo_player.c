#include "demo_player.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

typedef struct {
    ui_cmd_t cmd;
    uint32_t arg;
} demo_evt_t;

static QueueHandle_t s_q;

void demo_player_on_ui_cmd(ui_cmd_t cmd, uint32_t arg, void *ctx)
{
    (void)ctx;
    demo_evt_t e = { cmd, arg };
    if (s_q) {
        xQueueSend(s_q, &e, 0);                    /* LVGL task: never block here */
    }
}

static const struct {
    const char *title, *artist;
    uint32_t duration_ms;
} TRACKS[] = {
    { "Midnight City Lights", "The Neon Drivers", 214000 },
    { "A Very Long Track Title That Has To Scroll Across The Screen", "An Artist With A Long Name feat. Someone Else", 187000 },
    { "Short", "Solo", 95000 },
};
#define TRACK_COUNT (sizeof TRACKS / sizeof TRACKS[0])

static void demo_task(void *arg)
{
    (void)arg;
    size_t idx = 0;
    uint32_t pos = 0;
    bool playing = true;

    ui_show_track(TRACKS[idx].title, TRACKS[idx].artist, TRACKS[idx].duration_ms);
    ui_set_progress(pos, playing);

    for (;;) {
        demo_evt_t e;
        bool changed_track = false, changed_pos = false;

        if (xQueueReceive(s_q, &e, pdMS_TO_TICKS(1000)) == pdTRUE) {
            switch (e.cmd) {
            case UI_CMD_NEXT:
                idx = (idx + 1) % TRACK_COUNT;
                pos = 0;
                changed_track = true;
                break;
            case UI_CMD_PREV:
                if (pos > 3000) {
                    pos = 0;                         /* like Spotify: first restart the track */
                    changed_pos = true;
                } else {
                    idx = (idx + TRACK_COUNT - 1) % TRACK_COUNT;
                    pos = 0;
                    changed_track = true;
                }
                break;
            case UI_CMD_PLAY_PAUSE:
                playing = !playing;
                changed_pos = true;
                break;
            case UI_CMD_SEEK:
                pos = e.arg;
                changed_pos = true;
                break;
            case UI_CMD_SOURCE_NEXT:                 /* the demo has a single source */
            case UI_CMD_SOURCE_PREV:
                break;
            }
        } else if (playing) {
            pos += 1000;
            if (pos >= TRACKS[idx].duration_ms) {
                idx = (idx + 1) % TRACK_COUNT;
                pos = 0;
                changed_track = true;
            }
        }

        if (changed_track) {
            ui_show_track(TRACKS[idx].title, TRACKS[idx].artist, TRACKS[idx].duration_ms);
        }
        if (changed_track || changed_pos) {
            ui_set_progress(pos, playing);
        }
    }
}

void demo_player_start(void)
{
    s_q = xQueueCreate(8, sizeof(demo_evt_t));
    xTaskCreatePinnedToCore(demo_task, "demo", 4096, NULL, 3, NULL, 0);
}
