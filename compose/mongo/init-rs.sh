#!/usr/bin/env bash
# Replica Set initiieren und mongotUser mit Rolle searchCoordinator anlegen.
set -euo pipefail

MONGOT_USER="${MONGOT_USER:-mongotUser}"
PASSWORD_FILE="${PASSWORD_FILE:-/passwordFile}"
MONGO_HOST="${MONGO_HOST:-mongodb:27017}"
PASSWORD="$(cat "$PASSWORD_FILE")"

echo "Warte auf mongod (${MONGO_HOST}) …"
for i in $(seq 1 60); do
  if mongosh --quiet --host "$MONGO_HOST" --eval 'db.adminCommand({ ping: 1 }).ok' 2>/dev/null | grep -q 1; then
    break
  fi
  sleep 2
done

echo "rs.initiate …"
mongosh --quiet --host "$MONGO_HOST" <<EOF
try {
  rs.status();
} catch (e) {
  rs.initiate({
    _id: "rs0",
    members: [{ _id: 0, host: "mongodb:27017" }]
  });
}
EOF

echo "Warte auf PRIMARY …"
for i in $(seq 1 60); do
  STATE="$(mongosh --quiet --host "$MONGO_HOST" --eval 'try { rs.isMaster().ismaster } catch(e) { false }' 2>/dev/null || true)"
  if [[ "$STATE" == "true" ]]; then
    break
  fi
  sleep 2
done

echo "mongotUser anlegen …"
mongosh --quiet --host "$MONGO_HOST" <<EOF
const admin = db.getSiblingDB("admin");
const existing = admin.getUser("${MONGOT_USER}");
if (!existing) {
  admin.createUser({
    user: "${MONGOT_USER}",
    pwd: "${PASSWORD}",
    roles: ["searchCoordinator"]
  });
  print("created ${MONGOT_USER}");
} else {
  print("${MONGOT_USER} already exists");
}
EOF

echo "init-rs fertig."
