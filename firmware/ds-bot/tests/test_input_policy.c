#include <assert.h>
#include <stdio.h>
#include "input_policy.h"

static void held_cancel_without_release(void)
{
    for (int key = 0; key < 2; ++key) {
        input_policy_t p;
        input_policy_init(&p);
        assert(input_policy_edge(&p, key, true, true, 7, true, 100) == INPUT_NONE);
        assert(input_policy_poll(&p, true, 7, 2099) == INPUT_NONE);
        assert(input_policy_poll(&p, true, 7, 2100) == INPUT_CANCEL);
        assert(input_policy_poll(&p, true, 7, 9000) == INPUT_NONE);
        assert(input_policy_edge(&p, key, false, true, 7, true, 9001) == INPUT_NONE);
    }
}

static void short_press_and_terminal_transition(void)
{
    input_policy_t p;
    input_policy_init(&p);
    assert(input_policy_edge(&p, 0, true, true, 1, true, 0) == INPUT_NONE);
    assert(input_policy_edge(&p, 0, false, true, 1, true, 1000) == INPUT_NONE);
    assert(input_policy_poll(&p, true, 1, 3000) == INPUT_NONE);
    assert(input_policy_edge(&p, 1, true, true, 1, true, 4000) == INPUT_NONE);
    /* A completed task cannot turn its still-held cancel key into a new recording. */
    assert(input_policy_poll(&p, false, 1, 7000) == INPUT_NONE);
    assert(input_policy_edge(&p, 1, false, false, 1, true, 7001) == INPUT_NONE);
    assert(input_policy_edge(&p, 1, true, false, 1, true, 8000) == INPUT_RECORD_START);
    assert(input_policy_edge(&p, 1, false, false, 1, true, 8100) == INPUT_RECORD_STOP);
}

static void crossed_keys_and_task_identity(void)
{
    input_policy_t p;
    input_policy_init(&p);
    assert(input_policy_edge(&p, 0, true, true, 3, true, 0) == INPUT_NONE);
    assert(input_policy_edge(&p, 1, true, true, 3, true, 500) == INPUT_NONE);
    assert(input_policy_poll(&p, true, 3, 2000) == INPUT_CANCEL);
    assert(input_policy_edge(&p, 0, false, true, 3, true, 2100) == INPUT_NONE);
    assert(input_policy_poll(&p, true, 3, 2500) == INPUT_NONE);
    assert(input_policy_edge(&p, 1, false, true, 3, true, 2600) == INPUT_NONE);
    assert(input_policy_edge(&p, 0, true, true, 3, true, 3000) == INPUT_NONE);
    assert(input_policy_poll(&p, true, 4, 6000) == INPUT_NONE);
    assert(input_policy_edge(&p, 0, false, true, 4, true, 6001) == INPUT_NONE);
}

static void only_recording_owner_can_release(void)
{
    input_policy_t p;
    input_policy_init(&p);
    assert(input_policy_edge(&p, 0, true, false, 0, true, 0) == INPUT_RECORD_START);
    assert(input_policy_edge(&p, 1, true, false, 0, false, 10) == INPUT_NONE);
    assert(input_policy_edge(&p, 1, false, false, 0, false, 20) == INPUT_NONE);
    assert(input_policy_edge(&p, 0, false, false, 0, false, 30) == INPUT_RECORD_STOP);
    assert(input_policy_edge(&p, 0, false, false, 0, true, 40) == INPUT_NONE);
}

static void clock_wraparound(void)
{
    input_policy_t p;
    input_policy_init(&p);
    uint32_t start = UINT32_MAX - 1000;
    assert(input_policy_edge(&p, 1, true, true, 8, true, start) == INPUT_NONE);
    assert(input_policy_poll(&p, true, 8, start + 1999u) == INPUT_NONE);
    assert(input_policy_poll(&p, true, 8, start + 2000u) == INPUT_CANCEL);
}

int main(void)
{
    held_cancel_without_release();
    short_press_and_terminal_transition();
    crossed_keys_and_task_identity();
    only_recording_owner_can_release();
    clock_wraparound();
    puts("PASS: hold threshold, release suppression, two-key ownership, task identity, clock wraparound");
}
