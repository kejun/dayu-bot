#pragma once
#include <stdbool.h>
#include <stdint.h>

#define VAD_MONITOR_MAX_MS 5000
#define VAD_UTTERANCE_MAX_MS 30000
#define VAD_RMS_ON 500
#define VAD_RMS_OFF 250
#define VAD_START_FRAMES 2
static inline bool vad_voice_start(unsigned *frames, int rms)
{
    *frames = rms >= VAD_RMS_ON ? *frames + 1 : 0;
    return *frames >= VAD_START_FRAMES;
}
typedef enum { VAD_MORE, VAD_SILENCE, VAD_LIMIT } vad_end_t;
static inline vad_end_t vad_end(bool utterance, uint32_t elapsed_ms, uint32_t silence_ms)
{
    if (silence_ms >= (utterance ? 1200u : 500u)) return VAD_SILENCE;
    if (elapsed_ms >= (utterance ? VAD_UTTERANCE_MAX_MS : VAD_MONITOR_MAX_MS)) return VAD_LIMIT;
    return VAD_MORE;
}
