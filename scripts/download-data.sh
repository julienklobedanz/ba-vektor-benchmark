#!/usr/bin/env bash
# Lädt VectorDBBench-Cohere-Parquets nach data/cohere_100k und data/cohere_1m.
# Quelle: https://assets.zilliz.com/benchmark/<dir>/
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE_URL="${BASE_URL:-https://assets.zilliz.com/benchmark}"
CHECKSUMS="$ROOT/data/SHA256SUMS"

download_one() {
  local remote_dir="$1"
  local local_dir="$2"
  shift 2
  local files=("$@")
  mkdir -p "$local_dir"
  for f in "${files[@]}"; do
    local dest="$local_dir/$f"
    local url="$BASE_URL/$remote_dir/$f"
    if [[ -f "$dest" ]]; then
      echo "  vorhanden: $dest"
      continue
    fi
    echo "  lade $url"
    curl -fL --retry 3 --retry-delay 2 -o "$dest.partial" "$url"
    mv "$dest.partial" "$dest"
  done
}

echo "==> Cohere Small 100K (VectorDBBench: cohere_small_100k)"
download_one "cohere_small_100k" "$ROOT/data/cohere_100k" \
  shuffle_train.parquet test.parquet neighbors.parquet

echo "==> Cohere Medium 1M (VectorDBBench: cohere_medium_1m)"
download_one "cohere_medium_1m" "$ROOT/data/cohere_1m" \
  shuffle_train.parquet test.parquet neighbors.parquet

echo "==> SHA256"
mkdir -p "$ROOT/data"
if [[ ! -f "$CHECKSUMS" ]]; then
  (
    cd "$ROOT/data"
    find cohere_100k cohere_1m -type f -name '*.parquet' | sort | xargs sha256sum
  ) > "$CHECKSUMS"
  echo "  Checksummen geschrieben: $CHECKSUMS (bitte committen)"
else
  echo "  prüfe gegen $CHECKSUMS"
  (cd "$ROOT/data" && sha256sum -c SHA256SUMS)
fi

echo "Fertig."
