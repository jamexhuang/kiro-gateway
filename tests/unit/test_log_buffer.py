from kiro.log_buffer import LogRingBuffer


def test_ring_buffer_keeps_last_n():
    buf = LogRingBuffer(capacity=3)
    for i in range(5):
        buf.append({"seq": i, "msg": f"line {i}", "level": "INFO", "ts": float(i)})
    entries = buf.snapshot()
    assert len(entries) == 3
    assert [e["seq"] for e in entries] == [2, 3, 4]


def test_ring_buffer_since_seq():
    buf = LogRingBuffer(capacity=10)
    for i in range(5):
        buf.append({"seq": i, "msg": f"line {i}", "level": "INFO", "ts": 0.0})
    assert [e["seq"] for e in buf.since(2)] == [3, 4]
    assert buf.since(99) == []


def test_ring_buffer_subscribers_get_events():
    buf = LogRingBuffer(capacity=10)
    seen = []
    buf.subscribe(seen.append)
    buf.append({"seq": 0, "msg": "x", "level": "INFO", "ts": 0.0})
    assert len(seen) == 1


def test_ring_buffer_unsubscribe():
    buf = LogRingBuffer(capacity=10)
    seen = []
    buf.subscribe(seen.append)
    buf.unsubscribe(seen.append)
    buf.append({"seq": 0, "msg": "x", "level": "INFO", "ts": 0.0})
    assert len(seen) == 0


def test_ring_buffer_snapshot_with_limit():
    buf = LogRingBuffer(capacity=10)
    for i in range(8):
        buf.append({"seq": i, "msg": f"line {i}", "level": "INFO", "ts": 0.0})
    entries = buf.snapshot(limit=3)
    assert len(entries) == 3
    assert [e["seq"] for e in entries] == [5, 6, 7]
