from __future__ import annotations

from typing import Protocol, Sequence


class VectorAdapter(Protocol):
    name: str

    def connect(self) -> None: ...

    def reset(self) -> None: ...

    def create_schema(self, m: int = 16, ef_construction: int = 100) -> None: ...

    def load(
        self,
        ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        tenant_ids: Sequence[int],
        batch_size: int,
    ) -> None: ...

    def build_index(self, m: int, ef_construction: int) -> None: ...

    def wait_ready(self, timeout_s: int) -> None: ...

    def set_search_param(self, value: int) -> None: ...

    def search(self, vector: Sequence[float], k: int, tenant_id: int | None) -> list[int]: ...

    def index_size_bytes(self) -> int | None: ...

    def version_info(self) -> dict: ...

    def self_test(self, vector: Sequence[float], expected_id: int, k: int = 10) -> None:
        """Query = Vektor aus dem Datensatz → eigene ID muss Rang 1 sein."""
        hits = self.search(vector, k=k, tenant_id=None)
        if not hits or hits[0] != expected_id:
            raise AssertionError(
                f"{self.name} self-test failed: expected id={expected_id} at rank 1, got {hits[:5]}"
            )
