# ba-vektor-benchmark

Reproduzierbare Messung von Vektorsuche (k=10) in **PostgreSQL/pgvector**, **MongoDB (+ mongot)** und **Qdrant**.

Siehe [SPEC.md](SPEC.md) für Zweck, Abgrenzung und Abnahmekriterien.

## Setup (Ubuntu 24.04 VM)

```bash
git pull
./scripts/setup-vm.sh          # Docker, Python 3.12 venv, .env, Secrets, Image-Pull
./scripts/download-data.sh     # Cohere 100K + 1M nach data/
```

Nach dem ersten Login neu anmelden (Docker-Gruppe) oder `newgrp docker`.

`.env` aus `.env.example` kopieren (macht `setup-vm.sh` automatisch). Passwörter nur lokal; `secrets/` ist gitignored.

### Image-Digests

In `.env.example` stehen gepinnte Tags und Manifest-Digests (Stand 2026-10-16, Docker Hub). Nach `docker pull` verifizieren:

```bash
docker image inspect --format '{{index .RepoDigests 0}}' pgvector/pgvector:0.8.7-pg17
```

`scripts/collect-env.sh` und jede JSONL-Zeile übernehmen den Digest in die Metadaten (`image_digest` / `env_meta.image_digests`).

## Phase 0 – Ablauf

**Ein System zur Zeit** (Compose-Profile):

```bash
# Postgres
docker compose --env-file .env -f compose/docker-compose.yml --profile pg up -d
ss -tlnp | grep 5432   # nur 127.0.0.1

source .venv/bin/activate
PYTHONPATH=. python -m bench.run --config configs/phase0.yaml --system pg --dataset cohere_100k
# danach optional 1M:
PYTHONPATH=. python -m bench.run --config configs/phase0.yaml --system pg --dataset cohere_1m

docker compose --env-file .env -f compose/docker-compose.yml --profile pg down

# MongoDB + mongot
docker compose --env-file .env -f compose/docker-compose.yml --profile mongo up -d
# warten bis mongot healthy (GET :8080/health → SERVING)
PYTHONPATH=. python -m bench.run --config configs/phase0.yaml --system mongo --dataset cohere_100k
docker compose --env-file .env -f compose/docker-compose.yml --profile mongo down

# Qdrant
docker compose --env-file .env -f compose/docker-compose.yml --profile qdrant up -d
PYTHONPATH=. python -m bench.run --config configs/phase0.yaml --system qdrant --dataset cohere_100k
docker compose --env-file .env -f compose/docker-compose.yml --profile qdrant down
```

Client-Kerne (CCX23): `taskset -c 3` um den Python-Prozess legen, z. B.:

```bash
taskset -c "${CPUSET_CLIENT:-3}" env PYTHONPATH=. python -m bench.run ...
```

Ergebnisse: `results/raw/<datum>_<system>_phase0.jsonl`.

### MongoDB unter Speicherlimit (P0-6)

In `.env` z. B. Gesamtbudget 8 GB testen (zwei Aufteilungen):

```bash
# 50/50
MEM_LIMIT_MONGOD=4g MEM_LIMIT_MONGOT=4g WIREDTIGER_CACHE_GB=2 MONGOT_JVM_HEAP=2g
# 30/70  (Compose/Docker: Dezimal-„g“ vermeiden → Millions of bytes als m)
MEM_LIMIT_MONGOD=2400m MEM_LIMIT_MONGOT=5600m WIREDTIGER_CACHE_GB=1 MONGOT_JVM_HEAP=3g
```

Verhalten (inkl. Exit-Code 137 = OOM) in der JSONL-Spalte `error` und hier dokumentieren, sobald gemessen.

## CPU / RAM (Defaults CCX23)

| | Wert |
|---|---|
| DB `cpuset` | `0-2` |
| Client `taskset` | `3` |
| `MEM_LIMIT` | `10g` (ein Container); Mongo: `MEM_LIMIT_MONGOD`+`MEM_LIMIT_MONGOT` = 5g+5g |

**Container-Limit(e) + Client-Prozess ≤ Host-RAM.** CCX23 hat 16 GB; Default 10 GB Container lässt Spielraum für OS und den Python-Client auf dem Host.

Postgres: `shared_buffers` = 25 % von `MEM_LIMIT`, `effective_cache_size` = 75 %, `maintenance_work_mem` = 50 % (HNSW-Build), `jit=off`. Defaults (128 MB `shared_buffers`) wären „Failure to Optimize“ (Raasveldt).

## Indexparameter (Fairness)

HNSW, **m / maxEdges = 16**, **ef_construction / ef_construct / numEdgeCandidates = 100**, Cosine, keine Quantisierung.

## Abweichungen von Defaults / Doku

1. **ef_construction = 100 (statt 64)**  
   MongoDB Vector Search erlaubt `hnswOptions.numEdgeCandidates` nur im Bereich **100–3200** (Default 100); siehe [How to Index Fields for Vector Search – hnswOptions](https://www.mongodb.com/docs/vector-search/indexes/vector-search-type/).  
   Für Fairness setzen wir denselben Wert bei allen Systemen. Bei **pgvector** weicht das vom üblichen Beispielwert `ef_construction=64` ab; bei **Qdrant** vom früheren Planwert 64.

2. **MongoDB Phase 0 ohne Access Control / Keyfile**  
   Wie das [offizielle Docker-Minimalsetup](https://www.mongodb.com/docs/search/self-managed/current/installation/docker/) (explizit unsicher). Vertretbar, weil Ports nur auf `127.0.0.1` und die Hetzner-Firewall nur Port 22 erlaubt.

3. **Compose-Hostnamen**  
   Offizielle Beispiele nutzen `mongod.search-community` / `mongot-community.search-community`. Hier: Servicenamen `mongodb` und `mongot` im Compose-Netz.

4. **Qdrant HNSW-Config**  
   Wird bei Collection-Erstellung gesetzt (`create_schema(m=…, ef_construction=…)`). `wait_ready` pollt `status == green` und `optimizer_status` ok; `indexed_vectors_count/points_count` nur in `notes`.

5. **mongot JVM-Heap**  
   Offiziell per CLI `--jvm-flags "-Xms… -Xmx…"` ([Hardware Considerations](https://www.mongodb.com/docs/search/self-managed/current/resource-planning-sizing/hardware/)), nicht über Env-Var. In Compose als `command`.

6. **mongot-Healthcheck**  
   Das Search-Image enthält oft kein `curl`. Compose versucht `curl` → `wget` → bash `/dev/tcp` HTTP-GET; Antwort muss `SERVING` enthalten. Vom Host zusätzlich: `curl -s http://127.0.0.1:8080/health`.

7. **Postgres: `plan_cache_mode = force_custom_plan`**  
   psycopg bereitet wiederholte Queries serverseitig vor (prepare_threshold=5). Postgres kann danach einen generischen Plan nutzen, der `tenant_id` nicht kennt; bei der schiefen Mandantenverteilung führte das in Phase 0 zu exakter Sortierung über 50 % der Zeilen (p50 ≈ 199 ms statt ≈ 2 ms). Mit `force_custom_plan` plant Postgres jede Query mit den echten Parametern. Zusätzlich `ANALYZE` nach dem Bulk-Load.

## Datensatz

VectorDBBench-Aufbereitung von Cohere wikipedia-22-12, 768 dim, **COSINE**:

| Lokal | Remote-Verzeichnis | Dateien |
|---|---|---|
| `data/cohere_100k/` | `cohere_small_100k` | `shuffle_train.parquet`, `test.parquet`, `neighbors.parquet` |
| `data/cohere_1m/` | `cohere_medium_1m` | dito |

Quelle: `https://assets.zilliz.com/benchmark/…` (wie VectorDBBench `dataset.py`). SHA256 in `data/SHA256SUMS` (nach erstem Download committen).

## Phase 1

Configs `configs/s1.yaml`, `s2.yaml`, `s3.yaml` sind Stubs. Harness-Interfaces existieren; volle Sweeps/Wiederholungen folgen in Phase 1.
