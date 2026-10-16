# SPEC – Messumgebung „Vektordatenhaltung in PostgreSQL, MongoDB und Qdrant“

> Stellen mit **[PRÜFEN]** muss Cursor gegen die aktuelle offizielle Doku verifizieren, bevor Code entsteht.

---

## 1. Zweck und Abgrenzung

**Zweck:** Reproduzierbare Messung von Vektorsuche (k=10) in drei Systemen unter drei Szenarien (S1 kleine RAG-Wissensbasis, S2 Multi-Tenant mit Filterselektivität 1/10/50 %, S3 begrenzter RAM).

**Nicht-Ziele:** keine Quantisierung, kein Hybrid-/Volltext-Search, keine Cluster/Sharding, keine Managed Services, kein UI, keine Verwendung von VectorDBBench als Harness (nur dessen Datensatz).

**Phasen:**
- **Phase 0 – Machbarkeit (bis Mitte KW43):** Stack läuft, Daten laden, gefilterte Suche funktioniert in allen drei Systemen, MongoDB unter Speicherlimit getestet. → Abnahmekriterien in §12.
- **Phase 1 – Mess-Harness:** vollständige Sweeps, Wiederholungen, alle 21 Konfigurationen × ≥5 Wiederholungen.

Cursor baut **zuerst nur Phase 0**. Phase-1-Teile werden als Interfaces/Stubs angelegt, nicht implementiert.

---

## 2. Zielumgebung

| Punkt | Vorgabe |
|---|---|
| Host | Hetzner CCX23 (4 vCPU dediziert, 16 GB) für Phase 0; CCX33 (8 vCPU, 32 GB) bzw. HAW-VM für Phase 1 |
| OS | Ubuntu 24.04 LTS, x86_64 |
| Container | Docker Engine + Compose-Plugin (offizielles Docker-Repo, nicht Snap) |
| Client | Python 3.12 im venv **auf dem Host** (nicht im Container) |
| Entwicklung | lokal in Cursor → `git push` → auf VM `git pull`. Auf der VM wird kein Code editiert. |

**Ein System zur Zeit:** Während einer Messung läuft nur das gemessene System (Compose-Profile), die anderen sind gestoppt.

**CPU-Trennung:** DB-Container und Client bekommen getrennte Kerne (über `.env`):
- CCX23: DB `cpuset: "0-2"`, Client `taskset -c 3`
- CCX33: DB `cpuset: "0-5"`, Client `taskset -c 6-7`

---

## 3. Repo-Struktur

```
ba-vektor-benchmark/
├── README.md                 # Kurzanleitung: Setup, Phase-0-Ablauf
├── SPEC.md                   # diese Datei
├── .env.example              # Versionen, MEM_LIMIT, CPUSETs, Passwörter (Dummy)
├── .gitignore                # data/, results/raw/, .env, secrets/
├── compose/
│   ├── docker-compose.yml    # Profile: pg, mongo, qdrant
│   ├── postgres/postgresql.conf
│   ├── mongo/mongod.conf
│   ├── mongo/mongot.conf
│   └── mongo/init-rs.sh      # Replica Set initiieren, mongot-User anlegen
├── scripts/
│   ├── setup-vm.sh           # Docker, Python, ufw nicht nötig (Hetzner-Firewall), Swap aus
│   ├── download-data.sh      # Datensatz nach data/
│   └── collect-env.sh        # Host-/Versions-Metadaten als JSON
├── bench/
│   ├── requirements.txt      # gepinnte Versionen
│   ├── data.py               # Parquet laden, Mandanten zuweisen, Ground Truth
│   ├── adapters/base.py      # Interface (§8)
│   ├── adapters/pgvector.py
│   ├── adapters/mongodb.py
│   ├── adapters/qdrant.py
│   ├── metrics.py            # Recall, Latenz-Perzentile, QPS
│   ├── memwatch.py           # Docker-/cgroup-Speicher sampeln
│   └── run.py                # CLI
├── configs/
│   ├── phase0.yaml
│   ├── s1.yaml  s2.yaml  s3.yaml   # Phase 1 (Stubs)
├── data/                     # gitignored
└── results/
    ├── raw/                  # gitignored, JSONL
    └── README.md
```

---

## 4. Dienste (docker-compose.yml)

**Allgemein für alle Dienste:**
- Images mit **festem Tag**; beim ersten Pull Digest ermitteln und in `.env` bzw. `results/`-Metadaten festhalten (`docker image inspect --format '{{index .RepoDigests 0}}'`).
- Ports **nur** als `127.0.0.1:<port>:<port>`.
- Daten in **benannten Volumes** (Neustart ohne Neuaufbau, wichtig für S3 Cold/Warm).
- `mem_limit: ${MEM_LIMIT}` und `cpuset: ${CPUSET_DB}` aus `.env`; `memswap_limit` = `mem_limit` (kein Swap).
- Healthcheck je Dienst.

### 4.1 PostgreSQL + pgvector (Profil `pg`)
- Image: `pgvector/pgvector:0.8.7-pg17` (iterative scans seit 0.8.0; Digest in `.env.example`)
- Port 5432
- `postgresql.conf` (Werte relativ zu `MEM_LIMIT`, per Template/Entrypoint gesetzt):
  - `shared_buffers` = 25 % von MEM_LIMIT
  - `effective_cache_size` = 75 % von MEM_LIMIT
  - `maintenance_work_mem` = so groß, dass der HNSW-Build in den Speicher passt, aber ≤ 50 % MEM_LIMIT (Wert dokumentieren)
  - `max_parallel_maintenance_workers` = Anzahl DB-Kerne − 1
  - `jit = off`
- Begründung im README: Defaults (128 MB shared_buffers) wären „Failure to Optimize“ (Raasveldt).

### 4.2 MongoDB + mongot (Profil `mongo`)
Grundlage: offizielle Anleitung „Install MongoDB Search with Docker“ (self-managed).
- Images: `mongodb/mongodb-community-server:8.3.13-ubi8` und `mongodb/mongodb-community-search:1.70.5` — **kein `latest`** (Doku: MongoDB 8.3+ für on-prem Search; Digests in `.env.example`)
- Replica Set `rs0` mit einem Member, Hostname `mongodb:27017` im Compose-Netz.
- `mongod.conf` (aus der Doku übernehmen, Hosts an Compose-Servicenamen anpassen):
  ```yaml
  net: { port: 27017, bindIpAll: true }
  replication: { replSetName: rs0 }
  setParameter:
    searchIndexManagementHostAndPort: mongot:27028
    mongotHost: mongot:27028
    skipAuthenticationToSearchIndexManagementServer: false
    useGrpcForSearch: true
    searchTLSMode: disabled
  storage:
    wiredTiger:
      engineConfig:
        cacheSizeGB: <aus MEM_LIMIT abgeleitet, siehe §11 offene Entscheidung>
  ```
- `mongot.conf`: syncSource → `mongodb:27017`, scramAuth mit User `mongotUser`, `passwordFile: /passwordFile`, `storage.dataPath: /data/mongot`, gRPC `mongot:27028`, healthCheck `:8080`, metrics `:9946`.
- `init-rs.sh`: `rs.initiate(...)` mit Member `mongodb:27017`, dann `db.getSiblingDB("admin").createUser({user:"mongotUser", pwd:..., roles:["searchCoordinator"]})`. Passwort aus `secrets/` (gitignored), Datei mit `echo -n` ohne Zeilenumbruch, `chmod 400`.
- Start-Reihenfolge: mongod healthy → init → mongot. mongot-Health: `GET :8080/health` = `SERVING` (heißt **nicht**, dass Indexe bereit sind).
- Client-Verbindung vom Host: `mongodb://127.0.0.1:27017/?directConnection=true` (sonst versucht der Treiber den Container-Hostnamen aufzulösen).
- Hinweis README: In Phase 0 ohne Access Control/Keyfile (wie Doku-Minimalsetup); vertretbar, weil Ports nur auf 127.0.0.1 und Hetzner-Firewall nur Port 22 erlaubt.
- mongot JVM-Heap: CLI `--jvm-flags "-Xms… -Xmx…"` (nicht Env); siehe offizielle Hardware-Doku. In Compose als `command`, Wert `MONGOT_JVM_HEAP`.

### 4.3 Qdrant (Profil `qdrant`)
- Image: `qdrant/qdrant:v1.19.2` (Digest in `.env.example`)
- Ports 6333 (REST), 6334 (gRPC); Client nutzt gRPC (`prefer_grpc=True`).
- Telemetrie aus (`QDRANT__TELEMETRY_DISABLED=true`).
- Vektoren standardmäßig im RAM; `on_disk`/mmap nur als S3-Variante (Phase 1).

---

## 5. Daten

- **Datensatz:** Cohere wikipedia-22-12, 768 Dim., **COSINE** – VectorDBBench („Cohere Medium 1M“ / „Cohere Small 100K“). Remote-Dirs: `cohere_medium_1m` / `cohere_small_100k`; Dateien: `shuffle_train.parquet`, `test.parquet`, `neighbors.parquet` von `https://assets.zilliz.com/benchmark/…`.
- `download-data.sh` lädt nach `data/cohere_1m/` und `data/cohere_100k/`, prüft Prüfsummen (SHA256 beim ersten Download erzeugen und ins Repo committen).
- Vektoren werden **einmal** L2-normalisiert; alle Systeme nutzen **Cosine**.
- **Query-Set:** die 1.000 Test-Queries des Datensatzes (bei weniger: alle).

### 5.1 Mandanten (`tenant_id`)
Ein einziges Feld `tenant_id` (Integer), seeded zufällig zugewiesen (`seed=42`), mit **schiefer Verteilung**, damit S2 mit einer Gleichheitsbedingung alle Selektivitäten abdeckt:

| Mandant | Anteil | Rolle in S2 |
|---|---|---|
| 1 | 50 % | Selektivität 50 % |
| 2 | 10 % | Selektivität 10 % |
| 3 | 1 % | Selektivität 1 % |
| 4 … 42 | je 1 % (39 Mandanten) | Füllmenge |

Filter in S2 ist immer `tenant_id == X`. Die Zuweisung wird als `data/<ds>/tenants.npy` gespeichert und von allen Adaptern identisch verwendet.

### 5.2 Ground Truth
- Ungefiltert: vorberechnete Nachbarn des Datensatzes verwenden, **zusätzlich** einmal selbst exakt nachrechnen und Übereinstimmung prüfen (Schutz gegen „Incorrect Code“).
- Gefiltert: je Mandant 1/2/3 exakte Top-10 per Brute Force (numpy, Batch-Matmul auf normalisierten Vektoren). Ablage `data/<ds>/gt_tenant_<X>.npy`.

---

## 6. Indexparameter (Fairness)

Für alle Systeme gleich: **HNSW, m = 16, ef_construction = 100, Cosine, keine Quantisierung.**

> **Abweichung vom ursprünglichen Plan (ef=64):** MongoDB erlaubt `numEdgeCandidates` nur im Bereich 100–3200 (Default 100); siehe [Vector Search Index – hnswOptions](https://www.mongodb.com/docs/vector-search/indexes/vector-search-type/). Deshalb einheitlich 100. Begründung auch im README unter „Abweichungen“.

| System | Build-Parameter | Such-Parameter (wird gesweept) | Filter-Unterstützung |
|---|---|---|---|
| pgvector | `USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=100)` | `hnsw.ef_search` | B-Tree auf `tenant_id`; `hnsw.iterative_scan = relaxed_order` |
| MongoDB | Vector-Search-Index: `similarity: cosine`, `numDimensions: 768`, `hnswOptions: {maxEdges: 16, numEdgeCandidates: 100}`; Feld `tenant_id` als `type: "filter"` | `numCandidates` | Pre-Filter über `filter` in `$vectorSearch` |
| Qdrant | `hnsw_config: {m: 16, ef_construct: 100}`, Distance Cosine | `hnsw_ef` | Payload-Index auf `tenant_id` (integer) vor dem Laden anlegen |

**Wichtig:** Systemeigenes Verhalten (z. B. Postgres-Planer wählt bei 1 % den B-Tree statt HNSW; Qdrant schaltet bei kleinen Filtermengen auf exakte Suche) wird **nicht** unterdrückt, sondern **protokolliert** (Postgres: `EXPLAIN` pro Konfiguration speichern; Qdrant: Parameter `full_scan_threshold` dokumentieren).

**Sweep-Werte (Phase 1):** `[10, 20, 40, 80, 120, 200, 400, 800]` für ef_search / hnsw_ef / numCandidates (bei MongoDB ≥ k). Betriebspunkt = kleinster Wert mit Recall@10 ≥ 0,95, sonst maximal erreichter Recall.

---

## 7. Laden und Index-Aufbau

- Batch-Insert (Batchgröße konfigurierbar, Default 1.000).
- Reihenfolge für alle gleich: **Collection/Tabelle + Filter-Index anlegen → Daten laden → Vektorindex bauen/abwarten**.
  - pgvector: HNSW nach dem Laden erzeugen.
  - MongoDB: Search-Index nach dem Laden anlegen, pollen bis Status `READY` (`$listSearchIndexes`).
  - Qdrant: Upload, dann pollen bis `status == green` und `indexed_vectors_count` ≈ Punktanzahl.
- Gemessen: `t_load`, `t_index`, `t_ready = t_load + t_index` (Zeit bis abfragebereit).

---

## 8. Adapter-Interface (`adapters/base.py`)

```python
class VectorAdapter(Protocol):
    name: str
    def connect(self) -> None: ...
    def reset(self) -> None: ...                    # Collection/Tabelle löschen
    def create_schema(self) -> None: ...            # inkl. Filter-Index
    def load(self, ids, vectors, tenant_ids, batch_size: int) -> None: ...
    def build_index(self, m: int, ef_construction: int) -> None: ...
    def wait_ready(self, timeout_s: int) -> None: ...
    def set_search_param(self, value: int) -> None: ...
    def search(self, vector, k: int, tenant_id: int | None) -> list[int]: ...
    def index_size_bytes(self) -> int | None: ...
    def version_info(self) -> dict: ...
```

Jeder Adapter enthält einen Selbsttest: Query = ein Vektor aus dem Datensatz → eigene ID muss Rang 1 sein.

---

## 9. Messablauf (`run.py`)

```
python -m bench.run --config configs/phase0.yaml --system pg|mongo|qdrant --dataset cohere_100k|cohere_1m
```

Pro Konfiguration:
1. Umgebung erfassen (`collect-env.sh`: Kernel, CPU, RAM, Docker-Version, Image-Digests, Git-Commit, `.env`-Werte ohne Secrets).
2. Laden + Index (§7), Zeiten speichern.
3. **Warm-up:** gesamtes Query-Set einmal ausführen, nicht werten.
4. Pro Sweep-Wert:
   - **Latenz:** Queries seriell (1 Client), p50/p95/p99, Recall@10.
   - **Durchsatz:** C parallele Client-Prozesse (multiprocessing, nicht Threads; Default C = Anzahl Client-Kerne × 2), feste Dauer 30 s, QPS.
5. Speicher während der Läufe mit 1 Hz sampeln (§10).
6. Wiederholungen: `repetitions` aus Config (Phase 0: 1, Phase 1: ≥ 5).

**Cold-Messung (Phase 1, S3):** Container neu starten + `sync; echo 3 > /proc/sys/vm/drop_caches`, dann **ohne** Warm-up messen. Run wird als `cache_state: cold|warm|hot` gekennzeichnet.

---

## 10. Metriken

| Metrik | Definition |
|---|---|
| Recall@10 | Mittel über Queries von \|Ergebnis ∩ GT-Top-10\| / 10 |
| Latenz p50/p95/p99 | Client-seitig gemessen, `time.perf_counter_ns`, inkl. Netzwerk-Roundtrip (localhost) |
| QPS | erfolgreiche Queries / Messdauer bei C Clients |
| t_load, t_index, t_ready | §7 |
| Indexgröße | pg: `pg_relation_size(index)`; MongoDB: Größe des mongot-Datenverzeichnisses (`du` auf Volume); Qdrant: Collection-Info + `du` auf Volume. Zusätzlich je System Gesamtgröße aller Volumes. |
| Speicher | je Container `memory.current` aus cgroup v2 und `docker stats` MemUsage; Peak und Median. Bei MongoDB **Summe** mongod + mongot. |
| Fehler | Timeouts, Exceptions, Exit-Codes (137 = OOM) – werden als Ergebnis gespeichert, nicht verworfen |

---

## 11. Ergebnisformat und Offenes

**Ergebnisse:** eine Zeile JSONL pro Messpunkt in `results/raw/<datum>_<system>_<szenario>.jsonl`:

```json
{"run_id":"…","ts":"…","git_commit":"…","system":"qdrant","system_version":"…","image_digest":"…",
 "scenario":"S2","dataset":"cohere_1m","tenant_id":3,"selectivity":0.01,
 "mem_limit_gb":16,"cpuset_db":"0-2","cache_state":"hot","rep":1,
 "m":16,"ef_construction":100,"search_param":120,
 "recall_at_10":0.962,"lat_p50_ms":1.8,"lat_p95_ms":3.1,"lat_p99_ms":4.4,"qps":1520,"clients":2,
 "t_load_s":412.0,"t_index_s":880.0,"index_bytes":3.4e9,"mem_peak_bytes":9.1e9,
 "error":null,"notes":"full_scan_threshold=10000"}
```

**Offene Entscheidungen (werden in Phase 0 geklärt und im README dokumentiert):**
1. **MongoDB-Speicherbudget in S3:** Das Limit gilt für mongod **und** mongot zusammen. Phase 0 testet zwei Aufteilungen (z. B. 50/50 und 30/70 mongod/mongot) bei 8 GB; die gewählte Aufteilung wird begründet.
2. **WiredTiger-Cache und mongot-Heap** konkret ableiten (`WIREDTIGER_CACHE_GB`, `MONGOT_JVM_HEAP`).
3. **Client-Parallelität C** final festlegen (CPU-Sättigung des Client-Kerns prüfen).

---

## 12. Abnahmekriterien Phase 0 (Machbarkeitsprüfung)

| # | Kriterium | Nachweis |
|---|---|---|
| P0-1 | `setup-vm.sh` richtet frische Ubuntu-24.04-VM ein; alle drei Profile starten, Healthchecks grün | Log |
| P0-2 | Ports nur auf 127.0.0.1 | `ss -tlnp` |
| P0-3 | MongoDB: Replica Set aktiv, mongot verbunden, Vector-Index mit Filterfeld `READY`, `$vectorSearch` mit `tenant_id`-Filter liefert 10 Treffer | Skriptausgabe |
| P0-4 | 100K-Datensatz: Laden, Index, gefilterte + ungefilterte Suche in allen drei Systemen, Recall gegen GT berechnet, Adapter-Selbsttest bestanden | JSONL |
| P0-5 | 1M-Datensatz: Laden + Index in allen drei Systemen, `t_ready` und Indexgröße erfasst, ein Messpunkt je Selektivität | JSONL |
| P0-6 | MongoDB mit `MEM_LIMIT` 8 GB und 4 GB: läuft oder bricht ab – Verhalten (inkl. Exit-Code) dokumentiert | JSONL + Notiz |
| P0-7 | Jede Ergebniszeile enthält Git-Commit und Image-Digest | JSONL |

**Ergebnis von Phase 0** fließt in die Rückmeldung an Prof. Zukunft (Mitte KW43) und entscheidet über Anmeldung bis 23.10. Greift die Rückfallregel (Vertrag Abschnitt 5), wird der Grund hier und im Vault dokumentiert.

---

## 13. Regeln für die Implementierung (für Cursor)

- Python: Typ-Hints, keine globalen Zustände, Konfiguration nur über YAML + `.env`.
- Abhängigkeiten in `requirements.txt` exakt pinnen (`==`): `psycopg[binary]`, `pgvector`, `pymongo`, `qdrant-client`, `numpy`, `pyarrow`, `pyyaml`, `tqdm`.
- Keine Optimierung, die nur ein System betrifft, ohne Eintrag im README („Overly-specific Tuning“).
- Keine Secrets im Repo; `.env.example` mit Dummywerten.
- Jede Abweichung von der offiziellen Doku eines Systems im Code kommentieren und im README auflisten.
- Kleine, nachvollziehbare Commits (Conventional Commits).
