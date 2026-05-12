import time
from kiro.metrics import MetricsRegistry


def test_metrics_bucket_rps_and_latency():
    reg = MetricsRegistry(window_s=60, bucket_s=1)
    now = time.time()
    for _ in range(10):
        reg.record(now, duration_s=0.2, is_error=False)
    for _ in range(2):
        reg.record(now, duration_s=1.5, is_error=True)
    series = reg.snapshot(now=now)
    assert series["count"] == 12
    assert series["errors"] == 2
    assert 0.0 < series["p50"] <= 1.5
    assert series["p95"] >= series["p50"]


def test_metrics_drops_old_buckets():
    reg = MetricsRegistry(window_s=5, bucket_s=1)
    t0 = 1_000_000.0
    reg.record(t0, 0.1, False)
    series = reg.snapshot(now=t0 + 10)
    assert series["count"] == 0


def test_metrics_multiple_buckets():
    reg = MetricsRegistry(window_s=60, bucket_s=5)
    t0 = 1_000_000.0
    reg.record(t0, 0.1, False)
    reg.record(t0 + 6, 0.2, True)  # different bucket
    series = reg.snapshot(now=t0 + 6)
    assert series["count"] == 2
    assert series["errors"] == 1
    assert len(series["series"]) == 2


def test_metrics_empty_snapshot():
    reg = MetricsRegistry(window_s=60, bucket_s=5)
    series = reg.snapshot(now=time.time())
    assert series["count"] == 0
    assert series["p50"] == 0.0
    assert series["p95"] == 0.0
