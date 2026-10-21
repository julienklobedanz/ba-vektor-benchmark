from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

DIM = 768
TENANT_SEED = 42
# Schiefe Verteilung §5.1: Mandant 1=50%, 2=10%, 3=1%, 4..42 je 1%
TENANT_WEIGHTS: dict[int, float] = {1: 0.50, 2: 0.10, 3: 0.01}
for _t in range(4, 43):
    TENANT_WEIGHTS[_t] = 0.01


def dataset_dir(root: Path, name: str) -> Path:
    mapping = {
        "cohere_100k": root / "data" / "cohere_100k",
        "cohere_1m": root / "data" / "cohere_1m",
    }
    if name not in mapping:
        raise ValueError(f"Unbekanntes Dataset: {name}")
    return mapping[name]


def _read_emb_column(path: Path, batch_size: int = 50_000) -> np.ndarray:
    """Liest die Embedding-Spalte batchweise direkt in ein float32-Array (n, DIM).

    Speicherschonend: Die Parquet-Datei speichert float64-Listen; ein Komplett-Read
    (Arrow-Tabelle + Kopien) braucht bei 1 Mio. Vektoren > 12 GB. Batchweise bleibt
    der Spitzenbedarf bei ~3 GB (Zielarray) + einem Batch.
    """
    pf = pq.ParquetFile(path)
    names = pf.schema_arrow.names
    col_name = "emb" if "emb" in names else names[-1]
    n = pf.metadata.num_rows
    out = np.empty((n, DIM), dtype=np.float32)
    pos = 0
    for batch in pf.iter_batches(batch_size=batch_size, columns=[col_name]):
        flat = batch.column(0).flatten().to_numpy(zero_copy_only=False)
        rows = len(batch)
        out[pos : pos + rows] = flat.reshape(rows, DIM)
        pos += rows
        del flat, batch
    if pos != n:
        raise ValueError(f"{path}: {pos} Zeilen gelesen, erwartet {n}")
    return out


def _read_id_column(path: Path) -> np.ndarray:
    table = pq.read_table(path, columns=["id"])
    if "id" not in table.column_names:
        raise KeyError(f"Spalte 'id' fehlt in {path}")
    return np.asarray(table.column("id").combine_chunks().to_numpy(), dtype=np.int64)


def _read_neighbors(path: Path) -> np.ndarray:
    table = pq.read_table(path)
    col = "neighbors_id" if "neighbors_id" in table.column_names else table.column_names[-1]
    arr = table.column(col).to_pylist()
    return np.asarray(arr, dtype=np.int64)


def l2_normalize(vectors: np.ndarray) -> np.ndarray:
    """In-place-Normalisierung (keine zweite Kopie des Datensatzes)."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    np.maximum(norms, 1e-12, out=norms)
    vectors /= norms
    return vectors


def assign_tenants(n: int, seed: int = TENANT_SEED) -> np.ndarray:
    tenants = np.array(sorted(TENANT_WEIGHTS.keys()), dtype=np.int32)
    weights = np.array([TENANT_WEIGHTS[t] for t in tenants], dtype=np.float64)
    weights = weights / weights.sum()
    rng = np.random.default_rng(seed)
    return rng.choice(tenants, size=n, p=weights).astype(np.int32)


def load_dataset(root: Path, name: str, *, normalize: bool = True) -> dict[str, Any]:
    d = dataset_dir(root, name)
    train_path = d / "shuffle_train.parquet"
    if not train_path.exists():
        train_path = d / "train.parquet"
    test_path = d / "test.parquet"
    gt_path = d / "neighbors.parquet"
    for p in (train_path, test_path, gt_path):
        if not p.exists():
            raise FileNotFoundError(f"Fehlt: {p} — scripts/download-data.sh ausführen")

    train = _read_emb_column(train_path)
    test = _read_emb_column(test_path)
    gt = _read_neighbors(gt_path)
    ids = _read_id_column(train_path)

    if normalize:
        train = l2_normalize(train)
        test = l2_normalize(test)

    n = train.shape[0]
    if len(ids) != n:
        raise ValueError(f"id-Spalte Länge {len(ids)} != train {n}")

    tenants_path = d / "tenants.npy"
    if tenants_path.exists():
        tenants = np.load(tenants_path)
        if len(tenants) != n:
            raise ValueError(f"tenants.npy Länge {len(tenants)} != {n}")
    else:
        tenants = assign_tenants(n)
        np.save(tenants_path, tenants)

    return {
        "name": name,
        "dir": d,
        "ids": ids,
        "train": train,
        "test": test,
        "gt": gt,
        "tenants": tenants,
        "dim": train.shape[1],
    }


def verify_ground_truth(
    train: np.ndarray,
    test: np.ndarray,
    gt: np.ndarray,
    ids: np.ndarray,
    *,
    k: int = 10,
    sample: int = 20,
    seed: int = 0,
) -> float:
    """Exakte Top-k per Batch-Matmul; Ergebnis-IDs (nicht Zeilenindizes) vs. Datensatz-GT."""
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(test), size=min(sample, len(test)), replace=False)
    matches = 0
    total = 0
    for i in idx:
        scores = train @ test[i]
        top = np.argpartition(-scores, kth=k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        ref = set(int(x) for x in gt[i][:k])
        got = set(int(x) for x in ids[top])
        matches += len(ref & got)
        total += k
    return matches / total if total else 0.0


def compute_filtered_gt(
    train: np.ndarray,
    test: np.ndarray,
    tenants: np.ndarray,
    tenant_id: int,
    ids: np.ndarray,
    *,
    k: int = 10,
    out_path: Path | None = None,
) -> np.ndarray:
    """Exakte Top-k je Query innerhalb des Mandanten; Rückgabe = Datensatz-IDs."""
    mask = tenants == tenant_id
    if not np.any(mask):
        raise ValueError(f"Keine Punkte für tenant_id={tenant_id}")
    subset_ids = ids[mask]
    subset = train[mask]
    k_eff = min(k, subset.shape[0])
    result = np.empty((test.shape[0], k_eff), dtype=np.int64)
    # Query-Chunks: Score-Matrix bleibt klein (z. B. 50 × 500k float32 ≈ 100 MB)
    chunk = 50
    for start in range(0, test.shape[0], chunk):
        end = min(start + chunk, test.shape[0])
        scores = test[start:end] @ subset.T
        np.negative(scores, out=scores)
        top_local = np.argpartition(scores, kth=k_eff - 1, axis=1)[:, :k_eff]
        row = np.arange(end - start)[:, None]
        ordered = top_local[row, np.argsort(scores[row, top_local], axis=1)]
        result[start:end] = subset_ids[ordered]
        del scores, top_local
    if out_path is not None:
        np.save(out_path, result)
    return result
