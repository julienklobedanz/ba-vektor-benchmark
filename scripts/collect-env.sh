#!/usr/bin/env bash
# Host-/Versions-Metadaten als JSON (ohne Secrets) auf stdout.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python3 - <<'PY'
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from pathlib import Path

ROOT = Path.cwd()


def run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


secret_re = re.compile(r"(PASSWORD|SECRET|PWD|TOKEN|KEY)", re.I)
raw_env = load_env(ROOT / ".env")
safe_env = {k: ("***" if secret_re.search(k) else v) for k, v in raw_env.items()}

git_commit = run(["git", "rev-parse", "HEAD"]) or None
os_pretty = ""
os_release = Path("/etc/os-release")
if os_release.exists():
    for line in os_release.read_text().splitlines():
        if line.startswith("PRETTY_NAME="):
            os_pretty = line.split("=", 1)[1].strip().strip('"')
            break

mem_total_kb = 0
meminfo = Path("/proc/meminfo")
if meminfo.exists():
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemTotal:"):
            mem_total_kb = int(line.split()[1])
            break

cpu_model = ""
cpuinfo = Path("/proc/cpuinfo")
if cpuinfo.exists():
    for line in cpuinfo.read_text().splitlines():
        if line.startswith("model name"):
            cpu_model = line.split(":", 1)[1].strip()
            break
if not cpu_model:
    cpu_model = run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor()

nproc = run(["nproc"]) or str(os.cpu_count() or 0)

digests: dict[str, dict[str, str]] = {}
for name, img_k, dig_k in [
    ("pgvector", "PGVECTOR_IMAGE", "PGVECTOR_DIGEST"),
    ("mongodb", "MONGO_IMAGE", "MONGO_DIGEST"),
    ("mongot", "MONGOT_IMAGE", "MONGOT_DIGEST"),
    ("qdrant", "QDRANT_IMAGE", "QDRANT_DIGEST"),
]:
    img = raw_env.get(img_k, "")
    dig = raw_env.get(dig_k, "")
    if img:
        live = run(["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", img])
        if live:
            dig = live
    digests[name] = {"image": img, "digest": dig}

doc = {
    "git_commit": git_commit,
    "os": os_pretty or platform.platform(),
    "kernel": platform.release(),
    "arch": platform.machine(),
    "cpu_model": cpu_model,
    "nproc": int(nproc),
    "mem_total_kb": mem_total_kb,
    "docker_version": run(["docker", "--version"]) or None,
    "compose_version": run(["docker", "compose", "version"]) or None,
    "env": safe_env,
    "image_digests": digests,
}
print(json.dumps(doc, indent=2))
PY
