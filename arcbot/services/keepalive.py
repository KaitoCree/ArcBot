"""Keepalive: insurance against Oracle Always Free idle reclamation (docs/DEPLOY.md).

Runs as its own low-priority systemd service (arcbot-keepalive), never inside the bot's event loop.

On A1 shapes memory above 20% on its own keeps the instance non-idle, so the main lever is a memory hold:
real, resident anonymous memory sized so the WHOLE box sits near `memory_hold_percent`, never above
`memory_ceiling_percent`, shrinking first if the box is short of memory. An optional hourly CPU burst
(nice 19, one core) self-tunes weekly and is off by default.

    python -m arcbot.services.keepalive            run
    python -m arcbot.services.keepalive --report   print the current 7-day estimate and exit
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("arcbot.keepalive")

CHUNK = 64 * 1024 * 1024  # hold memory in 64 MB pieces
PAGE = 4096
WEEK_S = 7 * 24 * 3600
BURST_CODE = """
import time
end = time.time() + %d
x = 0
while time.time() < end:
    for _ in range(200000):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
"""


# --------------------------------------------------------------------- pure
def desired_hold(total: int, available: int, held: int, cfg: dict[str, Any]) -> int:
    """Bytes the hold should have so used ~= target, capped by the ceiling, shrinking under pressure."""
    used = total - available
    others = max(0, used - held)  # everything that isn't our hold
    target = int(total * float(cfg.get("memory_hold_percent", 26)) / 100)
    ceiling = int(total * float(cfg.get("memory_ceiling_percent", 40)) / 100)
    want = max(0, min(target, ceiling) - others)
    # memory pressure: if less than 15% would stay available, give it back first
    floor_free = int(total * 0.15)
    if available - (want - held) < floor_free:
        want = max(0, held - (floor_free - available))
    return want


def round_chunks(nbytes: int) -> int:
    return int(nbytes // CHUNK)


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, math.ceil(0.95 * len(s)) - 1)
    return s[k]


def tune_burst(current: int, fraction_above: float, cfg: dict[str, Any]) -> int:
    b = cfg.get("burst_minutes_per_hour", {})
    lo, hi = int(b.get("min", 4)), int(b.get("max", 20))
    target = float(cfg.get("target_fraction_of_minutes_above", 0.12))
    if fraction_above < target:
        return min(hi, current + 2)
    if fraction_above > target * 1.8:
        return max(lo, current - 1)
    return current


# ----------------------------------------------------------------- /proc
def meminfo(path: str = "/proc/meminfo") -> tuple[int, int]:
    vals: dict[str, int] = {}
    with open(path) as f:
        for line in f:
            k, v = line.split(":", 1)
            vals[k] = int(v.strip().split()[0]) * 1024
    return vals["MemTotal"], vals.get("MemAvailable", vals.get("MemFree", 0))


def cpu_times(path: str = "/proc/stat") -> tuple[int, int]:
    with open(path) as f:
        parts = [int(x) for x in f.readline().split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    return sum(parts), idle


# ----------------------------------------------------------------- holder
class MemoryHold:
    def __init__(self) -> None:
        self.chunks: list[bytearray] = []

    @property
    def held(self) -> int:
        return len(self.chunks) * CHUNK

    def resize(self, n_chunks: int) -> None:
        while len(self.chunks) > n_chunks:
            self.chunks.pop()
        while len(self.chunks) < n_chunks:
            buf = bytearray(CHUNK)
            self._touch(buf)
            self.chunks.append(buf)

    @staticmethod
    def _touch(buf: bytearray) -> None:
        # write to every page so it is really resident (bytearray zero pages may be lazily mapped)
        mv = memoryview(buf)
        for i in range(0, len(buf), PAGE):
            mv[i] = 1

    def retouch(self) -> None:
        for c in self.chunks:
            self._touch(c)


# ----------------------------------------------------------------- runner
@dataclass
class Sample:
    ts: float
    cpu: float
    mem: float


class Keepalive:
    def __init__(self, cfg: dict[str, Any], state_dir: Path):
        self.cfg = cfg
        self.state_dir = state_dir
        self.samples_file = state_dir / "keepalive-samples.jsonl"
        self.state_file = state_dir / "keepalive-state.json"
        self.hold = MemoryHold()
        self.samples: list[Sample] = []
        self.state: dict[str, Any] = {"burst": int(cfg.get("burst_minutes_per_hour", {}).get("start", 8)),
                                      "last_tune": 0.0, "last_report": 0.0}
        self._stop = False
        self._burst_proc: subprocess.Popen | None = None
        self._prev_cpu = cpu_times()

    def load(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_file.exists():
            try:
                self.state.update(json.loads(self.state_file.read_text()))
            except ValueError:
                pass
        if self.samples_file.exists():
            cutoff = time.time() - WEEK_S
            for line in self.samples_file.read_text().splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d["ts"] >= cutoff:
                    self.samples.append(Sample(d["ts"], d["cpu"], d["mem"]))

    def save(self) -> None:
        self.state_file.write_text(json.dumps(self.state))
        cutoff = time.time() - WEEK_S
        self.samples = [s for s in self.samples if s.ts >= cutoff]
        tmp = self.samples_file.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(s.__dict__) + "\n" for s in self.samples))
        tmp.replace(self.samples_file)

    def sample(self) -> Sample:
        total, idle = cpu_times()
        pt, pi = self._prev_cpu
        self._prev_cpu = (total, idle)
        dt = max(1, total - pt)
        cpu = 100.0 * (1 - (idle - pi) / dt)
        mt, ma = meminfo()
        s = Sample(time.time(), round(cpu, 1), round(100.0 * (mt - ma) / mt, 1))
        self.samples.append(s)
        return s

    def adjust_memory(self) -> None:
        if not self.cfg.get("memory_hold_enabled", True):
            self.hold.resize(0)
            return
        total, avail = meminfo()
        want = round_chunks(desired_hold(total, avail, self.hold.held, self.cfg))
        have = len(self.hold.chunks)
        if want < have or want - have >= 4:  # grow in >=256 MB steps, shrink immediately
            self.hold.resize(want)
            log.info("memory hold %d MB (box %.1f%% used)", self.hold.held // 2**20,
                     100.0 * (total - meminfo()[1]) / total)

    def burst(self, minutes: int) -> None:
        """Busy one core in a child process (inherits nice 19) so sampling keeps running meanwhile."""
        if self._burst_proc is not None and self._burst_proc.poll() is None:
            return
        code = BURST_CODE % (minutes * 60)
        self._burst_proc = subprocess.Popen([sys.executable, "-c", code])

    def report(self) -> str:
        week = [s for s in self.samples if s.ts >= time.time() - WEEK_S]
        if not week:
            return "keepalive: no samples yet"
        mem_avg = sum(s.mem for s in week) / len(week)
        thr = float(self.cfg.get("oracle_threshold_percent", 20))
        above = sum(1 for s in week if s.cpu > thr) / len(week)
        return (f"keepalive weekly: mem avg {mem_avg:.1f}% (min {min(s.mem for s in week):.1f}%), "
                f"p95 CPU est {p95([s.cpu for s in week]):.1f}%, {above:.0%} of minutes above {thr:.0f}% CPU, "
                f"burst {self.state['burst'] if self.cfg.get('cpu_burst_enabled') else 0} min/h, "
                f"hold {self.hold.held // 2**20} MB, {len(week)} samples")

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        try:
            os.nice(19)
        except (AttributeError, OSError):
            pass
        self.load()
        now0 = time.time()
        # first run: start the weekly clocks now instead of reporting on a single sample
        if not self.state["last_report"]:
            self.state["last_report"] = now0
        if not self.state["last_tune"]:
            self.state["last_tune"] = now0
        cpu_times_ok = False
        log.info("keepalive started (memory hold %s, cpu burst %s)", self.cfg.get("memory_hold_enabled", True),
                 self.cfg.get("cpu_burst_enabled", False))
        last_touch = last_burst = 0.0
        while not self._stop:
            if cpu_times_ok:
                self.sample()
            else:
                self._prev_cpu = cpu_times()  # the first interval would include our own start-up; skip it
                cpu_times_ok = True
            self.adjust_memory()
            now = time.time()
            if now - last_touch > 300:
                self.hold.retouch()
                last_touch = now
            if self.cfg.get("cpu_burst_enabled") and now - last_burst >= 3600:
                last_burst = now
                self.burst(int(self.state["burst"]))
            if self.cfg.get("self_tune", True) and now - self.state["last_tune"] >= WEEK_S and len(self.samples) > 1000:
                thr = float(self.cfg.get("oracle_threshold_percent", 20)) + float(self.cfg.get("safety_margin_percent", 15))
                frac = sum(1 for s in self.samples if s.cpu > thr) / len(self.samples)
                self.state["burst"] = tune_burst(int(self.state["burst"]), frac, self.cfg)
                self.state["last_tune"] = now
            if self.cfg.get("log_weekly_report", True) and now - self.state["last_report"] >= WEEK_S:
                log.info(self.report())
                self.state["last_report"] = now
            self.save()
            for _ in range(60):
                if self._stop:
                    break
                time.sleep(1)
        if self._burst_proc is not None and self._burst_proc.poll() is None:
            self._burst_proc.terminate()
        self.hold.resize(0)
        self.save()
        log.info("keepalive stopped")


def main(argv: list[str] | None = None) -> int:
    from ..config import ROOT, load_config

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="arcbot.services.keepalive")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)
    if not Path("/proc/meminfo").exists():
        print("keepalive needs Linux (/proc); nothing to do here.")
        return 0
    cfg = load_config().keepalive
    if not cfg.get("enabled", True):
        log.info("keepalive disabled in config; exiting")
        return 0
    ka = Keepalive(cfg, ROOT / "data")
    if args.report:
        ka.load()
        print(ka.report())
        return 0
    ka.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
