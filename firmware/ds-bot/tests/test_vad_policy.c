#include <assert.h>
#include "vad_policy.h"
int main(void)
{
    /* Natural pauses in the failed two-part command must not submit its head. */
    assert(vad_end(true, 3000, 900) == VAD_MORE);
    assert(vad_end(true, 6000, 1100) == VAD_MORE);
    assert(vad_end(true, 9000, 1200) == VAD_SILENCE);
    assert(vad_end(true, 5000, 0) == VAD_MORE);
    assert(vad_end(true, 30000, 0) == VAD_LIMIT);
    /* Processing retains the original short cancellation capture. */
    assert(vad_end(false, 1800, 500) == VAD_SILENCE);
    assert(vad_end(false, 5000, 0) == VAD_LIMIT);
    assert(vad_end(true, 30000, 1800) == VAD_SILENCE);
    unsigned frames = 0;
    assert(!vad_voice_start(&frames, 9000)); /* a button impulse is not speech */
    assert(!vad_voice_start(&frames, 20));
    assert(!vad_voice_start(&frames, 550)); /* distant speech below old 1200 gate */
    assert(vad_voice_start(&frames, 650));
    return 0;
}
