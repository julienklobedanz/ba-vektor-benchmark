from __future__ import annotations

from typing import Any, Sequence

import psycopg
from pgvector.psycopg import register_vector


class PgVectorAdapter:
    name = "pg"

    def __init__(
        self,
        dsn: str = "postgresql://bench:bench_dummy_pg@127.0.0.1:5432/bench",
        dim: int = 768,
        table: str = "items",
    ) -> None:
        self.dsn = dsn
        self.dim = dim
        self.table = table
        self._conn: psycopg.Connection | None = None
        self._ef_search = 100
        self._iterative_scan = "relaxed_order"

    def connect(self) -> None:
        self._conn = psycopg.connect(self.dsn, autocommit=True)
        register_vector(self._conn)
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f"SET hnsw.iterative_scan = {self._iterative_scan}")
            cur.execute(f"SET hnsw.ef_search = {self._ef_search}")

    def _cur(self) -> psycopg.Cursor:
        assert self._conn is not None
        return self._conn.cursor()

    def reset(self) -> None:
        with self._cur() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {self.table}")

    def create_schema(self, m: int = 16, ef_construction: int = 100) -> None:
        _ = (m, ef_construction)  # HNSW erst in build_index
        with self._cur() as cur:
            cur.execute(
                f"""
                CREATE TABLE {self.table} (
                    id BIGINT PRIMARY KEY,
                    tenant_id INTEGER NOT NULL,
                    embedding vector({self.dim}) NOT NULL
                )
                """
            )
            cur.execute(f"CREATE INDEX ON {self.table} (tenant_id)")

    def load(
        self,
        ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        tenant_ids: Sequence[int],
        batch_size: int,
    ) -> None:
        assert self._conn is not None
        sql = f"INSERT INTO {self.table} (id, tenant_id, embedding) VALUES (%s, %s, %s)"
        n = len(ids)
        with self._conn.cursor() as cur:
            for start in range(0, n, batch_size):
                end = min(start + batch_size, n)
                rows = [
                    (int(ids[i]), int(tenant_ids[i]), list(map(float, vectors[i])))
                    for i in range(start, end)
                ]
                cur.executemany(sql, rows)

    def build_index(self, m: int, ef_construction: int) -> None:
        with self._cur() as cur:
            cur.execute(
                f"""
                CREATE INDEX {self.table}_hnsw
                ON {self.table}
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = {int(m)}, ef_construction = {int(ef_construction)})
                """
            )

    def wait_ready(self, timeout_s: int) -> None:
        _ = timeout_s
        with self._cur() as cur:
            cur.execute(
                """
                SELECT 1 FROM pg_indexes
                WHERE tablename = %s AND indexname = %s
                """,
                (self.table, f"{self.table}_hnsw"),
            )
            if cur.fetchone() is None:
                raise RuntimeError("HNSW-Index fehlt")

    def set_search_param(self, value: int) -> None:
        self._ef_search = int(value)
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(f"SET hnsw.ef_search = {self._ef_search}")

    def search(self, vector: Sequence[float], k: int, tenant_id: int | None) -> list[int]:
        assert self._conn is not None
        vec = list(map(float, vector))
        with self._conn.cursor() as cur:
            if tenant_id is None:
                cur.execute(
                    f"""
                    SELECT id FROM {self.table}
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (vec, k),
                )
            else:
                cur.execute(
                    f"""
                    SELECT id FROM {self.table}
                    WHERE tenant_id = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (int(tenant_id), vec, k),
                )
            return [int(r[0]) for r in cur.fetchall()]

    def explain(self, vector: Sequence[float], k: int, tenant_id: int | None) -> str:
        assert self._conn is not None
        vec = list(map(float, vector))
        with self._conn.cursor() as cur:
            if tenant_id is None:
                cur.execute(
                    f"""
                    EXPLAIN (FORMAT TEXT)
                    SELECT id FROM {self.table}
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (vec, k),
                )
            else:
                cur.execute(
                    f"""
                    EXPLAIN (FORMAT TEXT)
                    SELECT id FROM {self.table}
                    WHERE tenant_id = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (int(tenant_id), vec, k),
                )
            return "\n".join(r[0] for r in cur.fetchall())

    def index_size_bytes(self) -> int | None:
        with self._cur() as cur:
            cur.execute("SELECT pg_relation_size(%s)", (f"{self.table}_hnsw",))
            row = cur.fetchone()
            return int(row[0]) if row else None

    def version_info(self) -> dict[str, Any]:
        with self._cur() as cur:
            cur.execute("SHOW server_version")
            pg_ver = cur.fetchone()[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            return {"postgres": pg_ver, "pgvector": row[0] if row else None}

    def self_test(self, vector: Sequence[float], expected_id: int, k: int = 10) -> None:
        hits = self.search(vector, k=k, tenant_id=None)
        if not hits or hits[0] != expected_id:
            raise AssertionError(
                f"{self.name} self-test failed: expected id={expected_id} at rank 1, got {hits[:5]}"
            )
