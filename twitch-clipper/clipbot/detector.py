"""Chat-rate spike detection.

Messages are bucketed per second. Every second the detector looks at the
number of messages in the trailing ``window`` seconds and compares it to a
rolling baseline (mean / stddev of that same count over the last ``history``
seconds of "normal" chat). A spike starts when the count clears all of:

* ``min_messages``           - absolute floor, so a dead chat can't "spike" on 3 msgs
* ``baseline_mean * ratio``  - relative jump
* ``mean + sensitivity*std`` - statistical outlier

A spike stays open until the rate falls back below 75% of the trigger
threshold (hysteresis) or ``max_duration`` is reached. The finished spike is
then returned, and the detector cools down for ``cooldown`` seconds.
Seconds spent inside a spike are not added to the baseline.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass


@dataclass
class Spike:
    start: float  # wall-clock: start of the window that triggered
    peak_time: float  # wall-clock second with the highest trailing count
    end: float  # wall-clock: when the rate calmed down
    peak: int  # messages in the window at the peak
    baseline: float  # baseline mean (messages / window) when it triggered
    threshold: float

    @property
    def intensity(self) -> float:
        return self.peak / max(self.baseline, 1.0)


class SpikeDetector:
    def __init__(
        self,
        window: int = 10,
        history: int = 300,
        sensitivity: float = 3.0,
        ratio: float = 2.0,
        min_messages: int = 10,
        warmup: int = 60,
        max_duration: int = 60,
        cooldown: int = 20,
    ):
        self.window = window
        self.history = history
        self.sensitivity = sensitivity
        self.ratio = ratio
        self.min_messages = min_messages
        self.warmup = warmup
        self.max_duration = max_duration
        self.cooldown = cooldown

        self._buckets: dict[int, int] = {}
        self._samples: deque[int] = deque(maxlen=history)
        self._active: dict | None = None
        self._cooldown_until = 0.0
        self.last_count = 0
        self.just_started = False  # True for the one tick on which a spike opened
        self.last_threshold = float(min_messages)

    # -- input -------------------------------------------------------------
    def add(self, ts: float, weight: int = 1) -> None:
        sec = int(ts)
        self._buckets[sec] = self._buckets.get(sec, 0) + weight

    # -- stats -------------------------------------------------------------
    def _window_count(self, sec: int) -> int:
        return sum(self._buckets.get(s, 0) for s in range(sec - self.window + 1, sec + 1))

    def baseline(self) -> tuple[float, float]:
        n = len(self._samples)
        if n == 0:
            return 0.0, 0.0
        mean = sum(self._samples) / n
        var = sum((x - mean) ** 2 for x in self._samples) / n
        return mean, math.sqrt(var)

    def threshold(self) -> float:
        mean, std = self.baseline()
        return max(float(self.min_messages), mean * self.ratio, mean + self.sensitivity * std)

    @property
    def warmed_up(self) -> bool:
        return len(self._samples) >= self.warmup

    @property
    def in_spike(self) -> bool:
        return self._active is not None

    # -- main tick ---------------------------------------------------------
    def update(self, now: float) -> Spike | None:
        """Call about once per second. Returns a Spike when one finishes."""
        sec = int(now)
        cutoff = sec - self.window - 2
        for s in [s for s in self._buckets if s < cutoff]:
            del self._buckets[s]

        count = self._window_count(sec)
        self.last_count = count
        self.just_started = False

        if self._active is not None:
            a = self._active
            if count > a["peak"]:
                a["peak"], a["peak_time"] = count, float(sec)
            calmed = count < a["threshold"] * 0.75
            too_long = sec - a["start"] >= self.max_duration
            if calmed or too_long:
                self._active = None
                self._cooldown_until = sec + self.cooldown
                return Spike(
                    start=a["start"],
                    peak_time=a["peak_time"],
                    end=float(sec),
                    peak=a["peak"],
                    baseline=a["baseline"],
                    threshold=a["threshold"],
                )
            return None

        threshold = self.threshold()
        self.last_threshold = threshold
        if self.warmed_up and sec >= self._cooldown_until and count >= threshold:
            mean, _ = self.baseline()
            self._active = {
                "start": float(sec - self.window + 1),
                "peak": count,
                "peak_time": float(sec),
                "baseline": mean,
                "threshold": threshold,
            }
            self.just_started = True
            return None

        self._samples.append(count)
        return None

    def flush(self, now: float) -> Spike | None:
        """Close an open spike (e.g. on shutdown)."""
        if self._active is None:
            return None
        a, self._active = self._active, None
        return Spike(a["start"], a["peak_time"], float(int(now)), a["peak"], a["baseline"], a["threshold"])
