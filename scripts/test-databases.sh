#!/usr/bin/env bash
# Runs the live database engine tests of the gateway (docs/architecture.md, Section 10.4,
# "What M4.13 builds"). The script starts MySQL, MariaDB, SQL Server, ClickHouse, MongoDB,
# PostgreSQL, and an SSH bastion in docker, sets the SAMA_TEST_* URLs, and runs pytest.
#
# A container that runs already is used again. The containers stay after the run. To remove
# them: docker rm -f sama-mysql sama-mariadb sama-mssql sama-clickhouse sama-mongo \
#   sama-pgvector sama-ssh
#
# Usage: scripts/test-databases.sh [pytest arguments]
set -euo pipefail

cd "$(dirname "$0")/.."

NETWORK=sama-test
MSSQL_PASSWORD='Sama_Test_Pw1!'
WORK="${RUNNER_TEMP:-/var/tmp}/sama-test-databases"
mkdir -p "$WORK/ssh-init"

docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null

start() {
  local name=$1
  shift
  if [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ]; then
    return
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --network "$NETWORK" "$@" >/dev/null
}

wait_for() {
  local label=$1
  shift
  for _ in $(seq 1 90); do
    if "$@" >/dev/null 2>&1; then
      echo "ready: $label"
      return
    fi
    sleep 2
  done
  echo "not ready after 180 s: $label" >&2
  exit 1
}

# The SSH key of the bastion user, and the init script that allows TCP forwarding.
if [ ! -f "$WORK/id_ed25519" ]; then
  ssh-keygen -q -t ed25519 -N "" -C sama-test -f "$WORK/id_ed25519"
fi
cat >"$WORK/ssh-init/forwarding.sh" <<'EOF'
#!/usr/bin/with-contenv bash
sed -i 's/^#\?AllowTcpForwarding.*/AllowTcpForwarding yes/' /config/sshd/sshd_config
EOF
chmod +x "$WORK/ssh-init/forwarding.sh"

start sama-pgvector -p 55434:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg17
start sama-mysql -p 53306:3306 -e MYSQL_ROOT_PASSWORD=rootpw mysql:8.4
start sama-mariadb -p 53307:3306 -e MARIADB_ROOT_PASSWORD=rootpw mariadb:11.4
start sama-mssql -p 51433:1433 -e ACCEPT_EULA=Y -e "MSSQL_SA_PASSWORD=$MSSQL_PASSWORD" \
  mcr.microsoft.com/mssql/server:2022-latest
start sama-clickhouse -p 58123:8123 -e CLICKHOUSE_USER=sama -e CLICKHOUSE_PASSWORD=samapw \
  -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 clickhouse/clickhouse-server:24.8
start sama-mongo -p 57017:27017 -e MONGO_INITDB_ROOT_USERNAME=root \
  -e MONGO_INITDB_ROOT_PASSWORD=rootpw mongo:7.0
# A bastion with another key (from an earlier work folder) is made again.
if ! docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' sama-ssh 2>/dev/null |
  grep -qxF "PUBLIC_KEY=$(cat "$WORK/id_ed25519.pub")"; then
  docker rm -f sama-ssh >/dev/null 2>&1 || true
fi
start sama-ssh -p 52222:2222 -e PUID=1000 -e PGID=1000 -e USER_NAME=sama \
  -e "PUBLIC_KEY=$(cat "$WORK/id_ed25519.pub")" -v "$WORK/ssh-init:/custom-cont-init.d:ro" \
  lscr.io/linuxserver/openssh-server:latest

# The checks use TCP: the first start of MySQL and MariaDB runs a server without a network.
wait_for postgres docker exec sama-pgvector pg_isready -h 127.0.0.1 -U postgres
wait_for mysql docker exec sama-mysql mysql -h127.0.0.1 -uroot -prootpw -e "select 1"
wait_for mariadb docker exec sama-mariadb mariadb -h127.0.0.1 -uroot -prootpw -e "select 1"
wait_for sqlserver docker exec sama-mssql /opt/mssql-tools18/bin/sqlcmd -C -S 127.0.0.1 \
  -U sa -P "$MSSQL_PASSWORD" -Q "select 1"
wait_for clickhouse curl -sf "http://localhost:58123/?user=sama&password=samapw&query=select%201"
wait_for mongodb docker exec sama-mongo mongosh --quiet -u root -p rootpw --eval "db.runCommand({ping: 1})"
wait_for ssh bash -c "ssh-keyscan -p 52222 localhost 2>/dev/null | grep -q ed25519"
wait_for ssh-forwarding docker exec sama-ssh grep -q "^AllowTcpForwarding yes" /config/sshd/sshd_config

export SAMA_TEST_MYSQL_URL=mysql://root:rootpw@localhost:53306
export SAMA_TEST_MARIADB_URL=mysql://root:rootpw@localhost:53307
export SAMA_TEST_MSSQL_URL="mssql://sa:${MSSQL_PASSWORD}@localhost:51433"
export SAMA_TEST_CLICKHOUSE_URL=http://sama:samapw@localhost:58123
export SAMA_TEST_MONGO_URL=mongodb://root:rootpw@localhost:57017
export SAMA_TEST_SSH_URL=ssh://sama@localhost:52222
export SAMA_TEST_SSH_KEY_FILE="$WORK/id_ed25519"
export SAMA_TEST_DATABASE_URL="${SAMA_TEST_DATABASE_URL:-postgresql://postgres:postgres@localhost:55434/postgres}"

PYTEST=(uv run pytest)
if [ -x .venv/bin/pytest ] && ! command -v uv >/dev/null; then
  PYTEST=(.venv/bin/pytest)
fi
"${PYTEST[@]}" -rs tests/db/test_database_engines.py tests/db/test_database_mongo.py \
  tests/db/test_database_tunnel.py tests/db/test_database_gateway.py "$@"
