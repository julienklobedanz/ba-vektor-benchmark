# Template: WIREDTIGER_CACHE_GB wird von render-mongod-conf.sh ersetzt.
# Hostnamen an Compose-Servicenamen angepasst (mongodb / mongot).
net:
  port: 27017
  bindIpAll: true

replication:
  replSetName: rs0

setParameter:
  searchIndexManagementHostAndPort: mongot:27028
  mongotHost: mongot:27028
  skipAuthenticationToSearchIndexManagementServer: false
  useGrpcForSearch: true
  searchTLSMode: disabled

storage:
  wiredTiger:
    engineConfig:
      cacheSizeGB: {{WIREDTIGER_CACHE_GB}}
