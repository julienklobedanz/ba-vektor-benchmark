from __future__ import annotations

import time
from typing import Any, Sequence

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel


class MongoDBAdapter:
    name = "mongo"

    def __init__(
        self,
        uri: str = "mongodb://127.0.0.1:27017/?directConnection=true",
        db_name: str = "bench",
        coll_name: str = "items",
        index_name: str = "vector_index",
        dim: int = 768,
    ) -> None:
        self.uri = uri
        self.db_name = db_name
        self.coll_name = coll_name
        self.index_name = index_name
        self.dim = dim
        self._client: MongoClient | None = None
        self._coll: Collection | None = None
        self._num_candidates = 100

    def connect(self) -> None:
        self._client = MongoClient(self.uri)
        self._client.admin.command("ping")
        self._coll = self._client[self.db_name][self.coll_name]

    def reset(self) -> None:
        assert self._client is not None
        self._client[self.db_name].drop_collection(self.coll_name)
        self._coll = self._client[self.db_name][self.coll_name]

    def create_schema(self, m: int = 16, ef_construction: int = 100) -> None:
        # HNSW-Parameter (m / ef_construction) erst in build_index (Search-Index).
        _ = (m, ef_construction)
        assert self._coll is not None
        self._coll.create_index("tenant_id")

    def load(
        self,
        ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        tenant_ids: Sequence[int],
        batch_size: int,
    ) -> None:
        assert self._coll is not None
        n = len(ids)
        for start in range(0, n, batch_size):
            end = min(start + batch_size, n)
            docs = [
                {
                    "_id": int(ids[i]),
                    "tenant_id": int(tenant_ids[i]),
                    "embedding": [float(x) for x in vectors[i]],
                }
                for i in range(start, end)
            ]
            self._coll.insert_many(docs, ordered=False)

    def build_index(self, m: int, ef_construction: int) -> None:
        assert self._coll is not None
        definition = {
            "fields": [
                {
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": self.dim,
                    "similarity": "cosine",
                    "indexingMethod": "hnsw",
                    "hnswOptions": {
                        "maxEdges": int(m),
                        "numEdgeCandidates": int(ef_construction),
                    },
                },
                {"type": "filter", "path": "tenant_id"},
            ]
        }
        model = SearchIndexModel(
            definition=definition,
            name=self.index_name,
            type="vectorSearch",
        )
        try:
            self._coll.create_search_index(model=model)
        except OperationFailure as e:
            # Index existiert bereits
            if "already exists" not in str(e).lower() and e.code != 68:
                raise

    def wait_ready(self, timeout_s: int) -> None:
        assert self._coll is not None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for idx in self._coll.list_search_indexes(self.index_name):
                status = str(idx.get("status") or "").upper()
                queryable = idx.get("queryable")
                # $listSearchIndexes: status READY bzw. queryable true
                if status == "READY" or queryable is True:
                    return
            time.sleep(2)
        raise TimeoutError(f"Search-Index {self.index_name} nicht READY in {timeout_s}s")

    def set_search_param(self, value: int) -> None:
        self._num_candidates = max(int(value), 1)

    def search(self, vector: Sequence[float], k: int, tenant_id: int | None) -> list[int]:
        assert self._coll is not None
        num_candidates = max(self._num_candidates, k)
        stage: dict[str, Any] = {
            "index": self.index_name,
            "path": "embedding",
            "queryVector": [float(x) for x in vector],
            "numCandidates": num_candidates,
            "limit": k,
        }
        if tenant_id is not None:
            stage["filter"] = {"tenant_id": {"$eq": int(tenant_id)}}
        pipeline = [{"$vectorSearch": stage}, {"$project": {"_id": 1}}]
        return [int(doc["_id"]) for doc in self._coll.aggregate(pipeline)]

    def index_size_bytes(self) -> int | None:
        # mongot-Daten liegen im Volume; Größe wird in run.py via `du` ergänzt.
        return None

    def version_info(self) -> dict[str, Any]:
        assert self._client is not None
        info = self._client.server_info()
        return {"mongodb": info.get("version"), "gitVersion": info.get("gitVersion")}

    def self_test(self, vector: Sequence[float], expected_id: int, k: int = 10) -> None:
        hits = self.search(vector, k=k, tenant_id=None)
        if not hits or hits[0] != expected_id:
            raise AssertionError(
                f"{self.name} self-test failed: expected id={expected_id} at rank 1, got {hits[:5]}"
            )
