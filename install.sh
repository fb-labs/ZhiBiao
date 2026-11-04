#!/usr/bin/env bash
# =============================================================================
# 智标一键安装 / 部署脚本（Linux）
#
#   ./install.sh                        # 自动判断：Docker 可用则 Docker，否则本地
#   ./install.sh --mode docker          # 强制 Docker
#   ./install.sh --mode local           # 强制本地开发
#   ./install.sh --ai-api-key sk-xxx --yes
#   ./install.sh --mode local --skip-frontend --no-start
# =============================================================================
set -uo pipefail

MODE="auto"
AI_API_KEY=""
AI_API_BASE=""
AI_MODEL=""
ADMIN_USER="admin"
ADMIN_PASSWORD="sxit@123456"
DB_NAME="zhibiao"
PG_USER="postgres"
PG_PASSWORD="postgres"
PG_PORT=5432
API_PORT=8000
SKIP_FRONTEND=0
NO_START=0
FORCE=0
YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)             MODE="$2"; shift 2 ;;
        --ai-api-key)       AI_API_KEY="$2"; shift 2 ;;
        --ai-api-base)      AI_API_BASE="$2"; shift 2 ;;
        --ai-model)         AI_MODEL="$2"; shift 2 ;;
        --admin-user)       ADMIN_USER="$2"; shift 2 ;;
        --admin-password)   ADMIN_PASSWORD="$2"; shift 2 ;;
        --db-name)          DB_NAME="$2"; shift 2 ;;
        --pg-user)          PG_USER="$2"; shift 2 ;;
        --pg-password)      PG_PASSWORD="$2"; shift 2 ;;
        --pg-port)          PG_PORT="$2"; shift 2 ;;
        --api-port)         API_PORT="$2"; shift 2 ;;
        --skip-frontend)    SKIP_FRONTEND=1; shift ;;
        --no-start)         NO_START=1; shift ;;
        --force)            FORCE=1; shift ;;
        --yes)              YES=1; shift ;;
        --help|-h)          head -20 "$0"; exit 0 ;;
        *) echo "  [X] 未知参数: $1"; exit 1 ;;
    esac
done

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/install-$(date +%Y%m%d-%H%M%S).log"

log()  { echo "[$(date +%H:%M:%S)] $1" >> "$LOG_FILE"; }
title() { echo ""; echo "======================================================================"; echo "  $1"; echo "======================================================================"; }
step()  { echo ""; echo "[$1] $2"; log "STEP $1 $2"; }
ok()    { echo "    [OK] $1"; log "OK   $1"; }
info()  { echo "    [..] $1"; log "INFO $1"; }
warn()  { echo "    [!]  $1"; log "WARN $1"; }
die()   { echo ""; echo "    [X]  $1"; echo ""; echo "    日志: $LOG_FILE"; log "FATAL $1"; exit 1; }

have() { command -v "$1" &>/dev/null; }
test_port() { timeout 1 bash -c "echo >/dev/tcp/127.0.0.1/$1" 2>/dev/null; }
wait_http() {
    local url="$1" timeout_s="$2"
    local deadline=$(( $(date +%s) + timeout_s ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null | grep -q '^[23]'; then
            return 0
        fi
        sleep 4; echo -n '.'
    done
    echo ''; return 1
}

write_root_env() {
    local dst="$ROOT/.env"
    if [ -f "$dst" ] && [ "$FORCE" -eq 0 ]; then
        info ".env 已存在，保留原文件（重建加 --force）"; return
    fi
    [ -f "$dst" ] && cp "$dst" "$dst.bak-$(date +%Y%m%d-%H%M%S)"
    local url="postgresql+asyncpg://${PG_USER}:${PG_PASSWORD}@127.0.0.1:${PG_PORT}/${DB_NAME}"
    sed -e "s|^ZHIBIAO_DATABASE_URL=.*|ZHIBIAO_DATABASE_URL=${url}|" \
        -e "s|^ZHIBIAO_PORT=.*|ZHIBIAO_PORT=${API_PORT}|" \
        -e "s|^ZHIBIAO_BOOTSTRAP_USERNAME=.*|ZHIBIAO_BOOTSTRAP_USERNAME=${ADMIN_USER}|" \
        -e "s|^ZHIBIAO_BOOTSTRAP_PASSWORD=.*|ZHIBIAO_BOOTSTRAP_PASSWORD=${ADMIN_PASSWORD}|" \
        "$ROOT/.env.example" > "$dst"
    [ -n "$AI_API_KEY" ]  && sed -i "s|^ZHIBIAO_AI_API_KEY=.*|ZHIBIAO_AI_API_KEY=${AI_API_KEY}|" "$dst"
    [ -n "$AI_API_BASE" ] && sed -i "s|^ZHIBIAO_AI_BASE_URL=.*|ZHIBIAO_AI_BASE_URL=${AI_API_BASE}|" "$dst"
    [ -n "$AI_MODEL" ]    && sed -i "s|^ZHIBIAO_AI_MODEL=.*|ZHIBIAO_AI_MODEL=${AI_MODEL}|" "$dst"
    ok ".env 已生成（数据库 127.0.0.1:$PG_PORT/$DB_NAME）"
}

write_docker_env() {
    local dst="$ROOT/docker/.env.docker"
    if [ -f "$dst" ] && [ "$FORCE" -eq 0 ]; then
        info "docker/.env.docker 已存在，保留原文件"; return
    fi
    [ -f "$dst" ] && cp "$dst" "$dst.bak-$(date +%Y%m%d-%H%M%S)"
    sed -e "s|^API_PORT=.*|API_PORT=${API_PORT}|" \
        -e "s|^ZHIBIAO_BOOTSTRAP_USERNAME=.*|ZHIBIAO_BOOTSTRAP_USERNAME=${ADMIN_USER}|" \
        -e "s|^ZHIBIAO_BOOTSTRAP_PASSWORD=.*|ZHIBIAO_BOOTSTRAP_PASSWORD=${ADMIN_PASSWORD}|" \
        -e "s|^POSTGRES_DB=.*|POSTGRES_DB=${DB_NAME}|" \
        -e "s|^POSTGRES_USER=.*|POSTGRES_USER=${PG_USER}|" \
        -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PG_PASSWORD}|" \
        -e "s|^POSTGRES_PORT=.*|POSTGRES_PORT=${PG_PORT}|" \
        "$ROOT/docker/.env.docker.example" > "$dst"
    [ -n "$AI_API_KEY" ]  && sed -i "s|^ZHIBIAO_AI_API_KEY=.*|ZHIBIAO_AI_API_KEY=${AI_API_KEY}|" "$dst"
    [ -n "$AI_API_BASE" ] && sed -i "s|^ZHIBIAO_AI_BASE_URL=.*|ZHIBIAO_AI_BASE_URL=${AI_API_BASE}|" "$dst"
    [ -n "$AI_MODEL" ]    && sed -i "s|^ZHIBIAO_AI_MODEL=.*|ZHIBIAO_AI_MODEL=${AI_MODEL}|" "$dst"
    ok "docker/.env.docker 已生成"
}

ensure_postgres() {
    if test_port "$PG_PORT"; then ok "PostgreSQL 已在 127.0.0.1:$PG_PORT 运行"; return 0; fi
    if have systemctl; then
        info "尝试 systemctl start postgresql …"
        sudo systemctl start postgresql 2>/dev/null || systemctl start postgresql 2>/dev/null || true
        sleep 3
    fi
    if test_port "$PG_PORT"; then ok "PostgreSQL 已启动"; return 0; fi
    local pg_ctl=""
    for d in /usr/pgsql-16/bin /usr/pgsql-15/bin /usr/lib/postgresql/16/bin /usr/lib/postgresql/15/bin; do
        [ -x "$d/pg_ctl" ] && pg_ctl="$d/pg_ctl" && break
    done
    if [ -n "$pg_ctl" ]; then
        local data_dir="/var/lib/pgsql/data"
        [ -d "/var/lib/pgsql/16/data" ] && data_dir="/var/lib/pgsql/16/data"
        if [ -d "$data_dir" ]; then
            sudo "$pg_ctl" -D "$data_dir" -l "$LOG_DIR/postgres.log" -w start 2>&1 | tee -a "$LOG_FILE" || true
            sleep 3
        fi
    fi
    if test_port "$PG_PORT"; then ok "PostgreSQL 已启动"; return 0; fi
    die "PostgreSQL 无法启动，请手动启动后重试（或改用 --mode docker）"
}

ensure_database() {
    have psql || { warn "找不到 psql，跳过建库；启动时若提示数据库不存在，请手动创建 $DB_NAME"; return 0; }
    export PGPASSWORD="$PG_PASSWORD"
    local run_psql="psql"
    if [ "$(id -un)" != "postgres" ] && have sudo; then
        run_psql="sudo -u postgres psql"
    fi
    local exists; exists=$($run_psql -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" 2>/dev/null || echo "")
    if [ "$exists" != "1" ]; then
        info "创建数据库 $DB_NAME …"
        $run_psql -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d postgres -c "CREATE DATABASE \"$DB_NAME\"" 2>&1 | tee -a "$LOG_FILE"
    fi
    ok "数据库 $DB_NAME 就绪"
}

install_local() {
    step "1/7" "检查 Python"
    have python3 || die "未找到 python3。请安装 Python 3.12+ 后重试"
    local ver; ver=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
    [ "$(printf '%s' "$ver" | awk -F. '{print $1*100+$2}')" -lt 312 ] && die "Python 版本过低: $ver（需要 3.12+）"
    ok "Python $ver"

    step "2/7" "创建虚拟环境并安装后端依赖"
    local venv_py="$ROOT/venv/bin/python"
    if [ ! -x "$venv_py" ]; then
        python3 -m venv "$ROOT/venv" 2>&1 | tee -a "$LOG_FILE"
        [ ! -x "$venv_py" ] && die "创建虚拟环境失败（Ubuntu 需要 python3-venv）"
    fi
    "$venv_py" -m pip install --disable-pip-version-check -e "$ROOT" 2>&1 | tail -5 | tee -a "$LOG_FILE"
    [ "${PIPESTATUS[0]}" -ne 0 ] && die "pip install -e . 失败"
    ok "后端依赖安装完成"

    step "3/7" "构建前端"
    if [ "$SKIP_FRONTEND" -eq 1 ]; then
        warn "已跳过前端构建（服务只提供 API）"
    elif ! have npm; then
        warn "未找到 npm，跳过前端构建。安装 Node.js 后执行：cd packages/web && npm install && npm run build"
    else
        (cd "$ROOT/packages/web" && { [ -d node_modules ] || npm install; } && npm run build) 2>&1 | tail -20 | tee -a "$LOG_FILE"
        [ "${PIPESTATUS[0]}" -ne 0 ] && die "前端构建失败"
        [ -f "$ROOT/web_dist/index.html" ] || die "未找到 web_dist/index.html"
        ok "前端构建完成"
    fi

    step "4/7" "准备 PostgreSQL"
    ensure_postgres

    step "5/7" "建库 + 生成 .env"
    ensure_database
    write_root_env

    step "6/7" "初始化数据表与管理员"
    info "管理员：$ADMIN_USER / $ADMIN_PASSWORD（首次启动时自动创建）"

    step "7/7" "启动服务"
    if [ "$NO_START" -eq 1 ]; then
        info "手动启动：$ROOT/venv/bin/python -m uvicorn zhibiao.app:app --port $API_PORT"
        return 0
    fi
    if test_port "$API_PORT"; then
        ok "端口 $API_PORT 已有服务在运行"; return 0
    fi
    export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
    nohup "$venv_py" -m uvicorn zhibiao.app:app --host 0.0.0.0 --port "$API_PORT" \
        >"$LOG_DIR/api.out.log" 2>"$LOG_DIR/api.err.log" &
    wait_http "http://127.0.0.1:$API_PORT/api/health" 90 && ok "API 已就绪" || \
        warn "API 未在 90 秒内就绪，见 $LOG_DIR/api.err.log"
}

install_docker() {
    step "1/5" "检查 Docker 引擎"
    if ! have docker; then
        warn "未找到 docker，安装：curl -fsSL https://get.docker.com | sh"
        return 1
    fi
    if ! docker info &>/dev/null; then
        warn "Docker daemon 不可用，请执行：sudo systemctl start docker"
        return 1
    fi
    ok "Docker 引擎可用"

    step "2/5" "生成环境文件"
    write_docker_env

    step "3/5" "构建并启动"
    docker compose -f "$ROOT/docker/docker-compose.yml" --env-file "$ROOT/docker/.env.docker" \
        --profile infra up -d --build 2>&1 | tee -a "$LOG_FILE"
    [ "${PIPESTATUS[0]}" -ne 0 ] && die "docker compose up 失败"
    ok "容器已创建"

    step "4/5" "等待健康检查"
    wait_http "http://127.0.0.1:$API_PORT/api/health" 480 && ok "API 已就绪" || \
        warn "API 未在 8 分钟内就绪，查看：docker compose -f docker/docker-compose.yml logs -f api"

    step "5/5" "完成"
    info "管理员：$ADMIN_USER / $ADMIN_PASSWORD（首次启动自动创建）"
    return 0
}

title "智标 一键安装 / 部署 (Linux)"
[ -f "$ROOT/pyproject.toml" ] || die "当前目录不是仓库根目录: $ROOT"
ok "仓库根目录: $ROOT"

if [ -z "$AI_API_KEY" ] && [ "$YES" -eq 0 ]; then
    echo ""
    echo "  提示：模型功能需要 AI API Key，可稍后在 .env 中补充。"
    read -r -p "  现在输入 API Key（直接回车跳过）: " key_input
    [ -n "$key_input" ] && AI_API_KEY="$(echo "$key_input" | tr -d '[:space:]')"
fi

case "$MODE" in
    docker) install_docker || die "Docker 方式不可用，可改用 --mode local" ;;
    local)  install_local ;;
    auto)
        info "模式：自动判断"
        install_docker || { warn "自动改用本地开发"; install_local; }
        ;;
    *) die "未知 --mode: $MODE" ;;
esac

title "完成"
echo "  站点：      http://127.0.0.1:$API_PORT/"
echo "  API 文档：  http://127.0.0.1:$API_PORT/docs"
echo "  登录账号：  $ADMIN_USER / $ADMIN_PASSWORD"
echo "  日志：      $LOG_FILE"
