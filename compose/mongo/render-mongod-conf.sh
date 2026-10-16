#!/usr/bin/env bash
set -euo pipefail
CACHE="${WIREDTIGER_CACHE_GB:-4}"
sed "s/{{WIREDTIGER_CACHE_GB}}/${CACHE}/" /etc/mongod.conf.tpl > /etc/mongod.conf
echo "mongod.conf: wiredTiger.cacheSizeGB=${CACHE}" >&2
