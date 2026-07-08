# pipeline/tests/test_window_state.py
"""
Unit tests for WindowState in pipeline/window.py.
Run: PYTHONPATH=. pytest pipeline/tests/test_window_state.py -v
"""
from pipeline.window import WindowState, ML_CHANNELS, WINDOW_SIZE


def _msg(fill: float = 1.0) -> dict:
    return {ch: fill for ch in ML_CHANNELS}


def test_not_full_initially():
    assert not WindowState().is_full()


def test_full_at_window_size():
    ws = WindowState()
    for _ in range(WINDOW_SIZE):
        ws.update(_msg())
    assert ws.is_full()


def test_not_full_at_window_size_minus_one():
    ws = WindowState()
    for _ in range(WINDOW_SIZE - 1):
        ws.update(_msg())
    assert not ws.is_full()


def test_to_array_shape():
    ws = WindowState()
    for _ in range(WINDOW_SIZE):
        ws.update(_msg())
    arr = ws.to_array()
    assert len(arr) == len(ML_CHANNELS)
    assert all(len(row) == WINDOW_SIZE for row in arr)


def test_channel_values_in_correct_order():
    ws = WindowState()
    for _ in range(WINDOW_SIZE):
        msg = {ch: float(i) for i, ch in enumerate(ML_CHANNELS)}
        ws.update(msg)
    arr = ws.to_array()
    assert all(v == 0.0 for v in arr[0])
    assert all(v == 1.0 for v in arr[1])


def test_missing_key_zero_fills_and_stays_synced():
    # All channels must advance together every tick (to_array()/is_full() assume
    # equal-length deques) — a missing reading zero-fills rather than skipping,
    # so one flaky channel can't desync from the rest and stall the window forever.
    ws = WindowState()
    msg = {ch: 1.0 for ch in ML_CHANNELS}
    del msg[ML_CHANNELS[0]]
    ws.update(msg)
    assert len(ws._channels[ML_CHANNELS[0]]) == 1
    assert ws._channels[ML_CHANNELS[0]][0] == 0.0
    assert len(ws._channels[ML_CHANNELS[1]]) == 1


def test_sliding_evicts_oldest():
    ws = WindowState()
    for i in range(WINDOW_SIZE):
        ws.update({ch: float(i) for ch in ML_CHANNELS})
    assert ws.to_array()[0][0] == 0.0
    ws.update({ch: 9999.0 for ch in ML_CHANNELS})
    assert ws.to_array()[0][0] == 1.0
