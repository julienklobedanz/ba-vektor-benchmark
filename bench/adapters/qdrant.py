from __future__ import annotations

import time
from typing import Any, Sequence

from qdrant_client import QdrantClient
from qdrant_client.http import models as rest


class QdrantAdapter:
    name = "qdrant"

    def __init__(
        self,
        url: str = "http://127.0.0.1:6333",
        collection: str = "items",
        dim: int = 768,
    ) -> None:
        self.url = url
        self.collection = collection
        self.dim = dim
        self._client: QdrantClient | None = None
        self._hnsw_ef = 100
        # Für JSONL-notes: indexed_vectors_count / points_count nach wait_ready
        self.index_coverage_note: str | None = None

    def connect(self) -> None:
        self._client = QdrantClient(url=self.url, prefer_grpc=True)

    def reset(self) -> None:
        assert self._client is not None
        if self._client.collection_exists(self.collection):
            self._client.delete_collection(self.collection)

    def create_schema(self, m: int = 16, ef_construction: int = 100) -> None:
        assert self._client is not None
        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=rest.VectorParams(
                size=self.dim,
                distance=rest.Distance.COSINE,
            ),
            hnsw_config=rest.HnswConfigDiff(
                m=int(m),
                ef_construct=int(ef_construction),
            ),
        )
        # Payload-Index vor dem Laden (SPEC §6)
        self._client.create_payload_index(
            collection_name=self.collection,
            field_name="tenant_id",
            field_schema=rest.PayloadSchemaType.INTEGER,
        )

    def load(
        self,
        ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        tenant_ids: Sequence[int],
        batch_size: int,
    ) -> None:
        assert self._client is not None
        n = len(ids)
        for start in range(0, n, batch_size):
            end = min(start + batch_size, n)
            points = [
                rest.PointStruct(
                    id=int(ids[i]),
                    vector=[float(x) for x in vectors[i]],
                    payload={"tenant_id": int(tenant_ids[i])},
                )
                for i in range(start, end)
            ]
            self._client.upsert(collection_name=self.collection, points=points, wait=True)

    def build_index(self, m: int, ef_construction: int) -> None:
        # HNSW-Config wurde in create_schema gesetzt; Indexierung läuft beim Upsert.
        _ = (m, ef_construction)

    def wait_ready(self, timeout_s: int) -> None:
        """Wartet auf status=green und optimizer_status=ok; Index-Anteil nur loggen."""
        assert self._client is not None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            info = self._client.get_collection(self.collection)
            status = getattr(info, "status", None)
            status_val = status.value if hasattr(status, "value") else str(status)
            opt = getattr(info, "optimizer_status", None)
            opt_ok = False
            if opt is None:
                opt_ok = True
            elif isinstance(opt, bool):
                opt_ok = opt
            elif hasattr(opt, "ok"):
                opt_ok = bool(opt.ok)
            else:
                opt_ok = str(opt).lower() in {"ok", "true", "optimizersstatus.ok"}

            indexed = getattr(info, "indexed_vectors_count", None)
            points = getattr(info, "points_count", None)
            if indexed is not None and points is not None and points > 0:
                self.index_coverage_note = (
                    f"indexed_vectors_count/points_count={indexed}/{points}"
                    f" ({indexed / points:.4f})"
                )
            elif indexed is not None and points is not None:
                self.index_coverage_note = (
                    f"indexed_vectors_count/points_count={indexed}/{points}"
                )

            if status_val == "green" and opt_ok:
                return
            time.sleep(2)
        raise TimeoutError(
            f"Qdrant Collection nicht green/optimizer ok in {timeout_s}s"
            + (f" ({self.index_coverage_note})" if self.index_coverage_note else "")
        )

    def set_search_param(self, value: int) -> None:
        self._hnsw_ef = int(value)

    def search(self, vector: Sequence[float], k: int, tenant_id: int | None) -> list[int]:
        assert self._client is not None
        query_filter = None
        if tenant_id is not None:
            query_filter = rest.Filter(
                must=[
                    rest.FieldCondition(
                        key="tenant_id",
                        match=rest.MatchValue(value=int(tenant_id)),
                    )
                ]
            )
        res = self._client.query_points(
            collection_name=self.collection,
            query=[float(x) for x in vector],
            limit=k,
            query_filter=query_filter,
            search_params=rest.SearchParams(hnsw_ef=self._hnsw_ef),
        )
        return [int(p.id) for p in res.points]

    def index_size_bytes(self) -> int | None:
        assert self._client is not None
        info = self._client.get_collection(self.collection)
        disk = getattr(info, "disk_data_size", None)
        if disk is not None:
            return int(disk)
        return None

    def version_info(self) -> dict[str, Any]:
        assert self._client is not None
        try:
            return dict(self._client.info())  # type: ignore[arg-type]
        except Exception:
            return {"url": self.url}

    def self_test(self, vector: Sequence[float], expected_id: int, k: int = 10) -> None:
        hits = self.search(vector, k=k, tenant_id=None)
        if not hits or hits[0] != expected_id:
            raise AssertionError(
                f"{self.name} self-test failed: expected id={expected_id} at rank 1, got {hits[:5]}"
            )
