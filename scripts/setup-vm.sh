#!/usr/bin/env bash
# Richtet eine frische Ubuntu 24.04 VM für Phase 0 ein (Docker, Python 3.12, Swap aus).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Swap aus"
if swapon --show | grep -q .; then
  sudo swapoff -a || true
fi
# Persistenz: Swap-Zeilen in fstab auskommentieren (nicht löschen)
if [[ -f /etc/fstab ]]; then
  sudo sed -i.bak -E 's|^([^#].*\sswap\s)|#\1|' /etc/fstab || true
fi

echo "==> Docker Engine + Compose-Plugin (offizielles Docker-Repo)"
if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER" || true
fi

echo "==> Python 3.12 + venv"
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-pip

if [[ ! -d .venv ]]; then
  python3.12 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r bench/requirements.txt

echo "==> .env und Secrets"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "  .env aus .env.example angelegt"
fi
mkdir -p secrets
if [[ ! -f secrets/mongot_password ]]; then
  # Passwort aus .env, ohne Newline (wie MongoDB-Doku: echo -n)
  # shellcheck disable=SC1091
  set -a; source .env; set +a
  echo -n "${MONGOT_PASSWORD:-bench_dummy_mongot}" > secrets/mongot_password
  chmod 400 secrets/mongot_password
  echo "  secrets/mongot_password angelegt"
fi

echo "==> Compose-Skripte ausführbar"
chmod +x compose/postgres/docker-entrypoint-wrapper.sh \
  compose/mongo/render-mongod-conf.sh \
  compose/mongo/init-rs.sh \
  scripts/*.sh

echo "==> Image-Digests nach Pull in results/ schreiben"
mkdir -p results
for pair in \
  "PGVECTOR_IMAGE:PGVECTOR_DIGEST" \
  "MONGO_IMAGE:MONGO_DIGEST" \
  "MONGOT_IMAGE:MONGOT_DIGEST" \
  "QDRANT_IMAGE:QDRANT_DIGEST"; do
  IFS=':' read -r IMG_VAR DIG_VAR <<< "$pair"
  # shellcheck disable=SC1091
  set -a; source .env; set +a
  IMG="${!IMG_VAR}"
  echo "  pull $IMG …"
  docker pull "$IMG"
  DIGEST="$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMG" 2>/dev/null || true)"
  echo "  $IMG -> $DIGEST"
  echo "$DIGEST" >> results/image_digests.txt
done

echo ""
echo "Setup fertig. Neu einloggen (docker-Gruppe) oder: newgrp docker"
echo "Profile starten z. B.: docker compose --env-file .env -f compose/docker-compose.yml --profile pg up -d"
