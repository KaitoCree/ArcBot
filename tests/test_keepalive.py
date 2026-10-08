from arcbot.services.keepalive import CHUNK, desired_hold, p95, round_chunks, tune_burst

GB = 1024**3
CFG = {"memory_hold_percent": 26, "memory_ceiling_percent": 40, "target_fraction_of_minutes_above": 0.12,
       "burst_minutes_per_hour": {"start": 8, "min": 4, "max": 20}}


def test_hold_tops_box_up_to_target():
    total = 23 * GB
    want = desired_hold(total, total - int(1.2 * GB), 0, CFG)  # idle box: 1.2 GB used
    assert abs((1.2 * GB + want) / total - 0.26) < 0.01


def test_hold_counts_itself_as_ours():
    total = 23 * GB
    held = 4 * GB
    want = desired_hold(total, total - int(5.2 * GB), held, CFG)  # 1.2 GB others + our 4 GB
    assert abs((1.2 * GB + want) / total - 0.26) < 0.01


def test_hold_shrinks_when_others_grow():
    total = 23 * GB
    want = desired_hold(total, total - 7 * GB, 0, CFG)  # others already above target
    assert want == 0


def test_hold_backs_off_under_memory_pressure():
    total = 23 * GB
    held = 5 * GB
    want = desired_hold(total, 2 * GB, held, CFG)  # only 2 GB available
    assert want < held


def test_round_chunks():
    assert round_chunks(CHUNK * 3 + 5) == 3


def test_p95():
    assert p95([float(i) for i in range(1, 101)]) == 95.0
    assert p95([]) == 0.0


def test_tune_burst():
    assert tune_burst(8, 0.05, CFG) == 10
    assert tune_burst(20, 0.0, CFG) == 20
    assert tune_burst(8, 0.30, CFG) == 7
    assert tune_burst(4, 0.9, CFG) == 4
    assert tune_burst(8, 0.15, CFG) == 8
