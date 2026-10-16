# Generiert aus MEM_LIMIT durch docker-entrypoint-wrapper.sh
listen_addresses = '*'
shared_buffers = {{SHARED_BUFFERS}}
effective_cache_size = {{EFFECTIVE_CACHE_SIZE}}
maintenance_work_mem = {{MAINTENANCE_WORK_MEM}}
max_parallel_maintenance_workers = {{MAX_PARALLEL_MAINTENANCE_WORKERS}}
jit = off
max_connections = 100
