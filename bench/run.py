from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from tqdm import tqdm

from bench.adapters import ADAPTERS
from bench.data import compute_filtered_gt, load_dataset, verify_ground_truth
from bench.memwatch import MemWatch
from bench.metrics import latency_percentiles_ms, recall_at_k

ROOT = Path(__file__).resolve().parents[1]

CONTAINER_NAMES = {
    "pg": ["bench-postgres"],
    "mongo": ["bench-mongodb", "bench-mongot"],
    "qdrant": ["bench-qdrant"],
}

SELECTIVITY = {1: 0.50, 2: 0.10, 3: 0.01}


def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k] = v
        os.environ.setdefault(k, v)
    return env


def collect_env() -> dict[str, Any]:
    script = ROOT / "scripts" / "collect-env.sh"
    out = subprocess.check_output(["bash", str(script)], text=True, cwd=ROOT)
    return json.loads(out)


def volume_du_bytes(volume_names: list[str]) -> int | None:
    total = 0
    found = False
    for vol in volume_names:
        try:
            out = subprocess.check_output(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{vol}:/data:ro",
                    "busybox:1.36",
                    "du",
                    "-sb",
                    "/data",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            total += int(out.split()[0])
            found = True
        except Exception:
            continue
    return total if found else None


def make_adapter(system: str, env: dict[str, str], dim: int):
    if system == "pg":
        user = env.get("POSTGRES_USER", "bench")
        password = env.get("POSTGRES_PASSWORD", "bench_dummy_pg")
        db = env.get("POSTGRES_DB", "bench")
        dsn = f"postgresql://{user}:{password}@127.0.0.1:5432/{db}"
        return ADAPTERS[system](dsn=dsn, dim=dim)
    if system == "mongo":
        return ADAPTERS[system](dim=dim)
    if system == "qdrant":
        return ADAPTERS[system](dim=dim)
    raise ValueError(system)


def run_latency(
    adapter,
    queries: np.ndarray,
    k: int,
    tenant_id: int | None,
    ground_truth: np.ndarray,
) -> dict[str, Any]:
    latencies: list[int] = []
    results: list[list[int]] = []
    errors = 0
    for i in range(len(queries)):
        t0 = time.perf_counter_ns()
        try:
            hits = adapter.search(queries[i], k=k, tenant_id=tenant_id)
            results.append(hits)
        except Exception:
            errors += 1
            results.append([])
        latencies.append(time.perf_counter_ns() - t0)
    metrics = latency_percentiles_ms(latencies)
    metrics["recall_at_10"] = recall_at_k(results, ground_truth, k=k)
    metrics["errors"] = errors
    return metrics


def run_throughput(
    adapter_factory,
    queries: np.ndarray,
    k: int,
    tenant_id: int | None,
    clients: int,
    duration_s: float,
    search_param: int,
) -> dict[str, Any]:
    """Durchsatz mit multiprocessing (Phase-0: serielle Messung wenn clients=1)."""
    import multiprocessing as mp

    def worker(qid: int, counter, stop_at: float) -> None:
        adapter = adapter_factory()
        adapter.connect()
        adapter.set_search_param(search_param)
        n = len(queries)
        i = qid
        local = 0
        while time.time() < stop_at:
            try:
                adapter.search(queries[i % n], k=k, tenant_id=tenant_id)
                local += 1
            except Exception:
                pass
            i += clients
        with counter.get_lock():
            counter.value += local

    if clients <= 1:
        adapter = adapter_factory()
        adapter.connect()
        adapter.set_search_param(search_param)
        n = len(queries)
        i = 0
        ok = 0
        t_end = time.time() + duration_s
        while time.time() < t_end:
            try:
                adapter.search(queries[i % n], k=k, tenant_id=tenant_id)
                ok += 1
            except Exception:
                pass
            i += 1
        return {"qps": ok / duration_s, "clients": 1}

    counter = mp.Value("i", 0)
    stop_at = time.time() + duration_s
    procs = [
        mp.Process(target=worker, args=(c, counter, stop_at)) for c in range(clients)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    return {"qps": counter.value / duration_s, "clients": clients}


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase-0/1 Vektor-Benchmark")
    parser.add_argument("--config", default="configs/phase0.yaml")
    parser.add_argument("--system", choices=["pg", "mongo", "qdrant"], required=True)
    parser.add_argument("--dataset", choices=["cohere_100k", "cohere_1m"], required=True)
    parser.add_argument("--skip-load", action="store_true", help="Index vorausgesetzt")
    parser.add_argument("--no-throughput", action="store_true")
    args = parser.parse_args()

    env = load_dotenv(ROOT / ".env")
    cfg = yaml.safe_load((ROOT / args.config).read_text())
    phase = cfg.get("phase", 0)
    k = int(cfg.get("k", 10))
    batch_size = int(cfg.get("batch_size", 1000))
    m = int(cfg.get("m", 16))
    ef_construction = int(cfg.get("ef_construction", 100))
    search_params = list(cfg.get("search_params", [100]))
    repetitions = int(cfg.get("repetitions", 1))
    warmup = bool(cfg.get("warmup", True))
    clients = int(cfg.get("clients", 2))
    throughput_s = float(cfg.get("throughput_duration_s", 30))
    timeout_index = int(cfg.get("index_ready_timeout_s", 7200))
    tenants_to_measure = list(cfg.get("tenants", [None, 1, 2, 3]))
    scenario = cfg.get("scenario", "phase0")

    print("Lade Dataset …")
    ds = load_dataset(ROOT, args.dataset)
    print(f"  train={len(ds['train'])} test={len(ds['test'])} dim={ds['dim']}")

    print("Prüfe ungefilterte Ground Truth (Stichprobe) …")
    agree = verify_ground_truth(
        ds["train"], ds["test"], ds["gt"], ds["ids"], k=k, sample=20
    )
    print(f"  Übereinstimmung Stichprobe: {agree:.3f}")
    if agree < 0.9:
        raise SystemExit(f"GT-Abweichung zu groß ({agree:.3f}) — Abbruch (Incorrect Code Schutz)")

    filtered_gt: dict[int, np.ndarray] = {}
    for tid in (1, 2, 3):
        out = ds["dir"] / f"gt_tenant_{tid}.npy"
        if out.exists():
            filtered_gt[tid] = np.load(out)
        else:
            print(f"Berechne gefilterte GT tenant={tid} …")
            filtered_gt[tid] = compute_filtered_gt(
                ds["train"],
                ds["test"],
                ds["tenants"],
                tid,
                ds["ids"],
                k=k,
                out_path=out,
            )

    env_meta = collect_env()
    git_commit = env_meta.get("git_commit")
    digest_key = {"pg": "pgvector", "mongo": "mongodb", "qdrant": "qdrant"}[args.system]
    image_digest = (
        env_meta.get("image_digests", {}).get(digest_key, {}) or {}
    ).get("digest")

    adapter = make_adapter(args.system, env, ds["dim"])
    adapter.connect()
    t_load = t_index = 0.0
    mem_peak_build_bytes: int | None = None

    mem = MemWatch(CONTAINER_NAMES[args.system])
    mem.start()

    if not args.skip_load:
        print("reset + schema …")
        adapter.reset()
        adapter.create_schema(m=m, ef_construction=ef_construction)
        print("load …")
        t0 = time.perf_counter()
        adapter.load(ds["ids"], ds["train"], ds["tenants"], batch_size=batch_size)
        t_load = time.perf_counter() - t0
        print(f"  t_load={t_load:.1f}s")
        print("build_index …")
        t0 = time.perf_counter()
        adapter.build_index(m=m, ef_construction=ef_construction)
        adapter.wait_ready(timeout_s=timeout_index)
        t_index = time.perf_counter() - t0
        print(f"  t_index={t_index:.1f}s  t_ready={t_load + t_index:.1f}s")
        mem_peak_build_bytes = mem.peak_and_reset()
        print(f"  mem_peak_build_bytes={mem_peak_build_bytes}")

    print("Selbsttest …")
    adapter.set_search_param(search_params[0])
    adapter.self_test(ds["train"][0], expected_id=int(ds["ids"][0]), k=k)
    print("  OK")

    index_bytes = adapter.index_size_bytes()
    if args.system == "mongo":
        index_bytes = volume_du_bytes(["ba-vektor-benchmark_mongot_data"]) or index_bytes
    elif args.system == "qdrant":
        vol = volume_du_bytes(["ba-vektor-benchmark_qdrant_data"])
        if vol is not None:
            index_bytes = vol

    version_info = adapter.version_info()
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = ROOT / "results" / "raw" / f"{day}_{args.system}_{scenario}.jsonl"

    if warmup:
        print("Warm-up (gesamtes Query-Set) …")
        for i in tqdm(range(len(ds["test"])), leave=False):
            adapter.search(ds["test"][i], k=k, tenant_id=None)

    def adapter_factory():
        return make_adapter(args.system, env, ds["dim"])

    for rep in range(1, repetitions + 1):
        for sp in search_params:
            adapter.set_search_param(sp)
            for tenant in tenants_to_measure:
                label = "unfiltered" if tenant is None else f"tenant={tenant}"
                print(f"Messung rep={rep} search_param={sp} {label}")
                if tenant is None:
                    gt = ds["gt"]
                else:
                    gt = filtered_gt[int(tenant)]

                err = None
                try:
                    lat = run_latency(adapter, ds["test"], k, tenant, gt)
                    thr = {"qps": None, "clients": clients}
                    if not args.no_throughput:
                        thr = run_throughput(
                            adapter_factory,
                            ds["test"],
                            k,
                            tenant,
                            clients=clients,
                            duration_s=throughput_s,
                            search_param=sp,
                        )
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    lat = {
                        "recall_at_10": None,
                        "lat_p50_ms": None,
                        "lat_p95_ms": None,
                        "lat_p99_ms": None,
                        "errors": 1,
                    }
                    thr = {"qps": None, "clients": clients}

                notes_parts = [f"gt_agreement_sample={agree:.3f}"]
                if args.system == "pg" and tenant is not None:
                    try:
                        notes_parts.append(
                            "explain="
                            + adapter.explain(ds["test"][0], k, tenant).replace("\n", " | ")
                        )
                    except Exception:
                        pass
                if args.system == "qdrant":
                    notes_parts.append("full_scan_threshold=default")
                    cov = getattr(adapter, "index_coverage_note", None)
                    if cov:
                        notes_parts.append(cov)

                mem_sum = mem.summary()
                row = {
                    "run_id": str(uuid.uuid4()),
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "git_commit": git_commit,
                    "system": args.system,
                    "system_version": version_info,
                    "image_digest": image_digest,
                    "scenario": scenario,
                    "dataset": args.dataset,
                    "tenant_id": tenant,
                    "selectivity": None if tenant is None else SELECTIVITY.get(int(tenant)),
                    "mem_limit_gb": _mem_limit_gb(env, args.system),
                    "cpuset_db": env.get("CPUSET_DB"),
                    "cache_state": "hot",
                    "rep": rep,
                    "m": m,
                    "ef_construction": ef_construction,
                    "search_param": sp,
                    "recall_at_10": lat.get("recall_at_10"),
                    "lat_p50_ms": lat.get("lat_p50_ms"),
                    "lat_p95_ms": lat.get("lat_p95_ms"),
                    "lat_p99_ms": lat.get("lat_p99_ms"),
                    "qps": thr.get("qps"),
                    "clients": thr.get("clients"),
                    "t_load_s": t_load,
                    "t_index_s": t_index,
                    "index_bytes": index_bytes,
                    "mem_peak_bytes": mem_sum.get("mem_peak_bytes"),
                    "mem_peak_build_bytes": mem_peak_build_bytes,
                    "error": err,
                    "notes": "; ".join(notes_parts),
                    "phase": phase,
                    "env_meta": {
                        "kernel": env_meta.get("kernel"),
                        "nproc": env_meta.get("nproc"),
                        "image_digests": env_meta.get("image_digests"),
                    },
                }
                append_jsonl(out_path, row)
                print(
                    f"  recall={row['recall_at_10']} p50={row['lat_p50_ms']} "
                    f"qps={row['qps']} err={err}"
                )

    mem.stop()
    print(f"Ergebnisse: {out_path}")


def _mem_limit_gb(env: dict[str, str], system: str) -> float | None:
    def parse(v: str) -> float:
        v = v.strip().lower()
        if v.endswith("g"):
            return float(v[:-1])
        if v.endswith("m"):
            return float(v[:-1]) / 1024
        return float(v)

    if system == "mongo":
        try:
            return parse(env.get("MEM_LIMIT_MONGOD", "0")) + parse(
                env.get("MEM_LIMIT_MONGOT", "0")
            )
        except Exception:
            return None
    v = env.get("MEM_LIMIT", "")
    try:
        return parse(v) if v else None
    except Exception:
        return None


if __name__ == "__main__":
    main()
