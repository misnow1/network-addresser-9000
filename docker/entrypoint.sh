#!/bin/sh
# Container entrypoint: wait for the database, apply migrations, ensure the
# RBAC groups exist (sync_roles — see README's "Setting up accounts"), then
# hand off to the real process (gunicorn) via exec so it becomes PID 1 and
# receives signals directly.
set -e

db_host="${DB_HOST:-127.0.0.1}"
db_port="${DB_PORT:-3306}"
max_attempts=30
attempt=0

echo "Waiting for database at ${db_host}:${db_port}..."
until python -c "
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1)
sys.exit(0 if s.connect_ex((host, port)) == 0 else 1)
" "${db_host}" "${db_port}"; do
    attempt=$((attempt + 1))
    if [ "${attempt}" -ge "${max_attempts}" ]; then
        echo "ERROR: database at ${db_host}:${db_port} not reachable after ${max_attempts} attempts." >&2
        exit 1
    fi
    sleep 1
done
echo "Database is reachable."

python manage.py migrate --noinput
python manage.py sync_roles

exec "$@"
