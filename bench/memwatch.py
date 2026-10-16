from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def container_id(name: str) -> str | None:
    try:
        return subprocess.check_output(
            ["docker", "inspect", "--format", "{{.Id}}", name],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def container_pid(name: str) -> int | None:
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.Pid}}", name],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        pid = int(out)
        return pid if pid > 0 else None
    except Exception:
        return None


def _read_cgroup_memory_current(cid: str | None, name: str) -> int | None:
    """Primär: cgroup v2 memory.current (Host-Pfad bzw. über Container-PID)."""
    candidates: list[Path] = []
    if cid:
        short = cid[:12]
        candidates.extend(
            [
                Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.current"),
                Path(f"/sys/fs/cgroup/system.slice/docker-{short}.scope/memory.current"),
                Path(f"/sys/fs/cgroup/docker/{cid}/memory.current"),
                Path(f"/sys/fs/cgroup/memory/docker/{cid}/memory.usage_in_bytes"),
            ]
        )
    pid = container_pid(name)
    if pid:
        cgroup_file = Path(f"/proc/{pid}/cgroup")
        if cgroup_file.exists():
            for line in cgroup_file.read_text().splitlines():
                # cgroup v2: "0::/system.slice/docker-….scope"
                if "::" in line:
                    path = line.split("::", 1)[1].strip()
                    if path:
                        candidates.insert(0, Path("/sys/fs/cgroup") / path.lstrip("/") / "memory.current")
                elif ":memory:" in line:
                    path = line.split(":memory:", 1)[1].strip()
                    candidates.insert(
                        0,
                        Path("/sys/fs/cgroup/memory") / path.lstrip("/") / "memory.usage_in_bytes",
                    )
    for p in candidates:
        if p.exists():
            try:
                return int(p.read_text().strip())
            except Exception:
                continue
    return None


def docker_stats_mem(container_name: str) -> int | None:
    """Fallback, wenn cgroup memory.current nicht lesbar ist."""
    try:
        out = subprocess.check_output(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.MemUsage}}",
                container_name,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        used = out.split("/")[0].strip()
        return _parse_size(used)
    except Exception:
        return None


def _parse_size(s: str) -> int | None:
    s = s.strip().replace("i", "")
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for u, mul in sorted(units.items(), key=lambda x: -len(x[0])):
        if s.upper().endswith(u):
            try:
                return int(float(s[: -len(u)].strip()) * mul)
            except ValueError:
                return None
    return None


@dataclass
class MemWatch:
    container_names: list[str]
    interval_s: float = 1.0
    samples: list[dict[str, Any]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def peak_and_reset(self) -> int | None:
        """Peak der bisherigen Samples, dann Samples leeren (z. B. nach Build)."""
        peak = self.summary().get("mem_peak_bytes")
        self.samples.clear()
        return peak

    def _loop(self) -> None:
        ids = {n: container_id(n) for n in self.container_names}
        while not self._stop.is_set():
            row: dict[str, Any] = {"ts": time.time(), "containers": {}, "source": {}}
            total = 0
            any_val = False
            for name in self.container_names:
                cid = ids.get(name)
                cgroup = _read_cgroup_memory_current(cid, name)
                stats = None
                source = "cgroup"
                if cgroup is not None:
                    val = cgroup
                else:
                    stats = docker_stats_mem(name)
                    val = stats
                    source = "docker_stats" if stats is not None else "none"
                row["containers"][name] = {"cgroup": cgroup, "docker_stats": stats}
                row["source"][name] = source
                if val is not None:
                    total += val
                    any_val = True
            row["sum_bytes"] = total if any_val else None
            self.samples.append(row)
            self._stop.wait(self.interval_s)

    def summary(self) -> dict[str, Any]:
        if not self.samples:
            return {"mem_peak_bytes": None, "mem_median_bytes": None, "n_samples": 0}
        vals = [s["sum_bytes"] for s in self.samples if s.get("sum_bytes") is not None]
        if not vals:
            return {
                "mem_peak_bytes": None,
                "mem_median_bytes": None,
                "n_samples": len(self.samples),
            }
        import numpy as np

        return {
            "mem_peak_bytes": int(max(vals)),
            "mem_median_bytes": int(np.median(vals)),
            "n_samples": len(self.samples),
        }

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(self.samples))
