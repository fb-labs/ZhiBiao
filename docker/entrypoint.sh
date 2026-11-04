#!/bin/sh
# ============================================================================
# 智标容器入口：等待数据库就绪后启动主进程
# ============================================================================
set -e

DB_TYPE="${ZHIBIAO_DB_TYPE:-postgresql}"
WAIT_TIMEOUT="${DB_WAIT_TIMEOUT:-90}"

wait_for() {
  host="$1"
  port="$2"
  name="$3"
  echo "[entrypoint] 等待 ${name} ${host}:${port} 就绪 ..."
  i=0
  while [ $i -lt "$WAIT_TIMEOUT" ]; do
    if python -c "import socket; s=socket.create_connection(('${host}', ${port}), timeout=2); s.close()" 2>/dev/null; then
      echo "[entrypoint] ${name} 已就绪（耗时 ${i}s）"
      return 0
    fi
    sleep 2
    i=$((i + 2))
  done
  echo "[entrypoint] 警告：等待 ${name} 超时（${WAIT_TIMEOUT}s）"
  return 1
}

case "$DB_TYPE" in
  sqlite)
    echo "[entrypoint] 使用 SQLite，跳过数据库等待"
    ;;
  mysql)
    wait_for "${ZHIBIAO_MYSQL_HOST:-mysql}" "${ZHIBIAO_MYSQL_PORT:-3306}" "MySQL" || {
      echo "[entrypoint] MySQL 不可达，终止启动"; exit 1;
    }
    ;;
  *)
    pg_host="postgres"
    pg_port="5432"
    if [ -n "$ZHIBIAO_DATABASE_URL" ]; then
      after_at=$(echo "$ZHIBIAO_DATABASE_URL" | sed 's#.*@##' | sed 's#/.*##')
      if [ -n "$after_at" ]; then
        pg_host=$(echo "$after_at" | cut -d: -f1)
        pg_port=$(echo "$after_at" | cut -d: -f2)
        [ -z "$pg_port" ] && pg_port="5432"
      fi
    fi
    wait_for "$pg_host" "$pg_port" "PostgreSQL" || {
      echo "[entrypoint] PostgreSQL 不可达，终止启动"; exit 1;
    }
    ;;
esac

echo "[entrypoint] 依赖就绪，启动：$@"
exec "$@"
