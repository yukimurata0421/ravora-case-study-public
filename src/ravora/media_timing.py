"""Audio-clock frame indices and a synthetic articulation replay."""

from ravora.speech import plan_mouth


def capture_tick(sample, origin, *, sample_rate=24000, fps=30):
    """Round the PCM position to the nearest video tick, with ties rounded up."""
    if any(type(value) is not int for value in (sample, origin, sample_rate, fps)):
        raise ValueError("EXPECTED_INTEGER_CLOCK_VALUES")
    if sample_rate <= 0 or fps <= 0 or origin < 0:
        raise ValueError("INVALID_CAPTURE_CLOCK")
    return max(0, (2 * (sample - origin) * fps + sample_rate) // (2 * sample_rate))


def indexed_video_timestamps(ticks, fps):
    """Attach explicit ticks to a baseline AVC stream with no reordered frames."""
    if type(fps) is not int or fps <= 0:
        raise ValueError("INVALID_CAPTURE_FPS")
    if not ticks or any(type(tick) is not int for tick in ticks):
        raise ValueError("INVALID_VIDEO_FRAME_INDEX")
    if ticks[0] < 0 or any(b <= a for a, b in zip(ticks, ticks[1:])):
        raise ValueError("INVALID_VIDEO_FRAME_INDEX")
    offset = ticks[0]
    expression = f"N+{offset}"
    for index, tick in enumerate(ticks):
        new_offset = tick - index
        if new_offset != offset:
            expression += f"+{new_offset - offset}*gte(N\\,{index})"
            offset = new_offset
    timestamp = f"({expression})/({fps}*TB)"
    return f"setts=pts={timestamp}:dts={timestamp}:duration=1/({fps}*TB)"


def reply_onset_gaps(frames, turns, origin, fps):
    """Measure intervals overlapping each audible onset, including a frame held across it."""
    gaps = []
    for index, turn in enumerate(turns):
        timing = turn.get('input_timing') or {}
        onset = timing.get('reply_signal_context_time')
        if onset is None:
            continue
        start = (onset - origin - .1) * fps
        end = (onset - origin + 1.) * fps
        intervals = [(b['tick'] - a['tick'], b['raf_ms'] - a['raf_ms'])
                     for a, b in zip(frames, frames[1:])
                     if a['tick'] < end and b['tick'] > start]
        if not intervals or not frames[0]['tick'] <= (onset-origin)*fps <= frames[-1]['tick']:
            raise ValueError('REPLY_ONSET_OUTSIDE_CAPTURE')
        gaps.append({'turn': index + 1, 'max_tick_gap': max(t for t, _ in intervals),
                     'max_raf_gap_ms': round(max(ms for _, ms in intervals), 1)})
    return gaps


def media_timing_replay():
    origin = 4800
    rate = 24000
    fps = 30
    samples = [4799, 5570, 6401, 8003, 8804]
    ticks = [capture_tick(frame, origin, sample_rate=rate, fps=fps) for frame in samples]
    assert ticks == [0, 1, 2, 4, 5]
    phones = [
        {"phone": "m", "start": 0., "end": .08},
        {"phone": "iy", "start": .08, "end": .21},
        {"phone": "b", "start": .21, "end": .28},
        {"phone": "uw", "start": .28, "end": .44},
        {"phone": "sil", "start": .44, "end": .60},
    ]
    plan = plan_mouth(phones, lead=.015, minimum_key=.045)
    assert [cue["shape"] for cue in plan] == ["closed", "I", "closed", "U", "closed"]
    return {
        "status": "PASS_MEDIA_TIMING_REPLAY",
        "synthetic_data": True,
        "pcm_sample_rate": rate,
        "capture_fps": fps,
        "source_ticks": ticks,
        "missing_source_ticks": [tick for tick in range(ticks[-1] + 1) if tick not in ticks],
        "video_timestamp_filter": indexed_video_timestamps(ticks, fps),
        "mouth_plan": plan,
        "external_api_calls": 0,
        "encoded_media_or_avatar_verified": False,
    }
