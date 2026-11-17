import random

from clipbot.detector import SpikeDetector


def feed(det, start, seconds, rate, rng):
    spikes = []
    for s in range(start, start + seconds):
        for _ in range(poisson(rng, rate)):
            det.add(s + rng.random())
        sp = det.update(s + 0.999)
        if sp:
            spikes.append(sp)
    return spikes


def poisson(rng, lam):
    # Knuth; fine for small lambdas
    import math
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def test_steady_chat_never_spikes():
    rng = random.Random(1)
    det = SpikeDetector()
    assert feed(det, 0, 1200, 3.0, rng) == []


def test_burst_is_detected_once_with_peak_inside():
    rng = random.Random(2)
    det = SpikeDetector()
    t0 = 1_000_000
    assert feed(det, t0, 300, 2.0, rng) == []
    spikes = feed(det, t0 + 300, 15, 15.0, rng)  # 7x burst
    spikes += feed(det, t0 + 315, 120, 2.0, rng)
    assert len(spikes) == 1
    sp = spikes[0]
    assert t0 + 290 <= sp.start <= t0 + 305
    assert t0 + 300 <= sp.peak_time <= t0 + 320
    assert sp.peak > 100
    assert sp.intensity > 4


def test_no_trigger_during_warmup():
    rng = random.Random(3)
    det = SpikeDetector(warmup=60)
    assert feed(det, 0, 20, 20.0, rng) == []


def test_min_messages_floor_on_dead_chat():
    det = SpikeDetector(min_messages=10, warmup=5)
    for s in range(100):
        det.update(s)
    for i in range(5):  # 5 messages in a dead chat is not a spike
        det.add(100.5)
    assert det.update(100.9) is None and not det.in_spike


def test_cooldown_blocks_back_to_back():
    rng = random.Random(4)
    det = SpikeDetector(cooldown=60)
    feed(det, 0, 300, 1.0, rng)
    a = feed(det, 300, 10, 20.0, rng) + feed(det, 310, 15, 1.0, rng)
    b = feed(det, 325, 10, 20.0, rng)  # inside cooldown
    assert len(a) == 1 and b == []


def test_max_duration_closes_long_spike():
    rng = random.Random(5)
    det = SpikeDetector(max_duration=30)
    feed(det, 0, 300, 1.0, rng)
    spikes = feed(det, 300, 100, 20.0, rng)
    assert spikes and spikes[0].end - spikes[0].start <= 31
