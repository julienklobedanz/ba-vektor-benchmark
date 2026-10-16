from bench.adapters.base import VectorAdapter
from bench.adapters.mongodb import MongoDBAdapter
from bench.adapters.pgvector import PgVectorAdapter
from bench.adapters.qdrant import QdrantAdapter

ADAPTERS: dict[str, type] = {
    "pg": PgVectorAdapter,
    "mongo": MongoDBAdapter,
    "qdrant": QdrantAdapter,
}

__all__ = [
    "ADAPTERS",
    "MongoDBAdapter",
    "PgVectorAdapter",
    "QdrantAdapter",
    "VectorAdapter",
]
