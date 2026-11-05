<#
    智标一键安装 / 部署脚本（Windows PowerShell）

    常用用法：
      .\scripts\install.ps1                          # 自动判断：Docker 可用则走 Docker，否则本地
      .\scripts\install.ps1 -Mode local              # 强制本地开发
      .\scripts\install.ps1 -Mode docker             # 强制 Docker
      .\scripts\install.ps1 -AiApiKey sk-xxx -Yes    # 非交互并写入模型 Key
      .\scripts\install.ps1 -SkipFrontend -NoStart   # 只准备环境

    页面与 API 由同一个 FastAPI 进程在同一端口提供。
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'docker', 'local')][string]$Mode = 'auto',
    [string]$AiApiKey = '',
    [string]$AiApiBase = '',
    [string]$AiModel = '',
    [string]$AdminUser = 'admin',
    [string]$AdminPassword = 'sxit@123456',
    [string]$DbName = 'zhibiao',
    [string]$PgUser = 'postgres',
    [string]$PgPassword = 'postgres',
    [int]$PgPort = 5432,
    [int]$ApiPort = 8000,
    [switch]$SkipFrontend,
    [switch]$NoStart,
    [switch]$Force,
    [switch]$Yes
)

$ErrorActionPreference = 'Continue'
# 脚本位于 scripts/，项目根目录是它的上一级。
$Root = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { (Get-Location).Path }
try { Set-Location $Root } catch { }

$LogDir = Join-Path $Root 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogFile = Join-Path $LogDir ('install-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

function Write-Log([string]$t) {
    try { Add-Content -Path $LogFile -Value ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $t) -Encoding UTF8 } catch { }
}
function Title([string]$t) {
    Write-Host ''
    Write-Host ('=' * 72) -ForegroundColor DarkGray
    Write-Host "  $t" -ForegroundColor White
    Write-Host ('=' * 72) -ForegroundColor DarkGray
    Write-Log "TITLE $t"
}
function Step([string]$n, [string]$t) { Write-Host ''; Write-Host "[$n] $t" -ForegroundColor Cyan; Write-Log "STEP $n $t" }
function Ok([string]$t) { Write-Host "    [OK] $t" -ForegroundColor Green; Write-Log "OK   $t" }
function Info([string]$t) { Write-Host "    [..] $t" -ForegroundColor Gray; Write-Log "INFO $t" }
function Warn([string]$t) { Write-Host "    [!]  $t" -ForegroundColor Yellow; Write-Log "WARN $t" }
function Die([string]$t) {
    Write-Host ''
    Write-Host "    [X]  $t" -ForegroundColor Red
    Write-Host ''
    Write-Host "    日志：$LogFile" -ForegroundColor DarkGray
    Write-Log "FATAL $t"
    exit 1
}

function Have([string]$cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Test-Port([string]$h, [int]$p) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect($h, $p, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(900)) { $c.Close(); return $false }
        $c.EndConnect($iar); $c.Close(); return $true
    } catch { return $false }
}
function Wait-Http([string]$url, [int]$timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 6
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400) { return $r.Content }
        } catch { }
        Start-Sleep -Seconds 4
        Write-Host '.' -NoNewline -ForegroundColor DarkGray
    }
    Write-Host ''
    return $null
}
function Write-Utf8NoBom([string]$path, [string[]]$lines) {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($path, $lines, $enc)
}

function Assert-Root {
    if (-not (Test-Path (Join-Path $Root 'pyproject.toml'))) {
        Die "当前目录不是仓库根目录：$Root（需要 pyproject.toml 与 zhibiao/ 在此）"
    }
    Ok "仓库根目录：$Root"
}

function Write-RootEnv {
    $dst = Join-Path $Root '.env'
    if ((Test-Path $dst) -and -not $Force) {
        Info '.env 已存在，保留原文件（要重建加 -Force）'
        return
    }
    if (Test-Path $dst) {
        Copy-Item $dst "$dst.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" -Force
    }
    $tpl = Get-Content (Join-Path $Root '.env.example') -Encoding UTF8
    $url = 'postgresql+asyncpg://{0}:{1}@127.0.0.1:{2}/{3}' -f $PgUser, $PgPassword, $PgPort, $DbName
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $tpl) {
        if ($line -match '^ZHIBIAO_DATABASE_URL=') { $out.Add("ZHIBIAO_DATABASE_URL=$url") }
        elseif ($line -match '^ZHIBIAO_PORT=') { $out.Add("ZHIBIAO_PORT=$ApiPort") }
        elseif (($line -match '^ZHIBIAO_AI_API_KEY=') -and $AiApiKey) { $out.Add("ZHIBIAO_AI_API_KEY=$AiApiKey") }
        elseif (($line -match '^ZHIBIAO_AI_BASE_URL=') -and $AiApiBase) { $out.Add("ZHIBIAO_AI_BASE_URL=$AiApiBase") }
        elseif (($line -match '^ZHIBIAO_AI_MODEL=') -and $AiModel) { $out.Add("ZHIBIAO_AI_MODEL=$AiModel") }
        elseif ($line -match '^ZHIBIAO_BOOTSTRAP_USERNAME=') { $out.Add("ZHIBIAO_BOOTSTRAP_USERNAME=$AdminUser") }
        elseif ($line -match '^ZHIBIAO_BOOTSTRAP_PASSWORD=') { $out.Add("ZHIBIAO_BOOTSTRAP_PASSWORD=$AdminPassword") }
        else { $out.Add($line) }
    }
    Write-Utf8NoBom $dst $out.ToArray()
    Ok ".env 已生成（数据库 127.0.0.1:$PgPort/$DbName）"
}

function Write-DockerEnv {
    $dst = Join-Path $Root 'docker\.env.docker'
    if ((Test-Path $dst) -and -not $Force) {
        Info 'docker\.env.docker 已存在，保留原文件（要重建加 -Force）'
        return
    }
    $tpl = Get-Content (Join-Path $Root 'docker\.env.docker.example') -Encoding UTF8
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $tpl) {
        if ($line -match '^API_PORT=') { $out.Add("API_PORT=$ApiPort") }
        elseif (($line -match '^ZHIBIAO_AI_API_KEY=') -and $AiApiKey) { $out.Add("ZHIBIAO_AI_API_KEY=$AiApiKey") }
        elseif (($line -match '^ZHIBIAO_AI_BASE_URL=') -and $AiApiBase) { $out.Add("ZHIBIAO_AI_BASE_URL=$AiApiBase") }
        elseif (($line -match '^ZHIBIAO_AI_MODEL=') -and $AiModel) { $out.Add("ZHIBIAO_AI_MODEL=$AiModel") }
        elseif ($line -match '^ZHIBIAO_BOOTSTRAP_USERNAME=') { $out.Add("ZHIBIAO_BOOTSTRAP_USERNAME=$AdminUser") }
        elseif ($line -match '^ZHIBIAO_BOOTSTRAP_PASSWORD=') { $out.Add("ZHIBIAO_BOOTSTRAP_PASSWORD=$AdminPassword") }
        elseif ($line -match '^POSTGRES_DB=') { $out.Add("POSTGRES_DB=$DbName") }
        elseif ($line -match '^POSTGRES_USER=') { $out.Add("POSTGRES_USER=$PgUser") }
        elseif ($line -match '^POSTGRES_PASSWORD=') { $out.Add("POSTGRES_PASSWORD=$PgPassword") }
        elseif ($line -match '^POSTGRES_PORT=') { $out.Add("POSTGRES_PORT=$PgPort") }
        else { $out.Add($line) }
    }
    Write-Utf8NoBom $dst $out.ToArray()
    Ok 'docker\.env.docker 已生成'
}

function Resolve-PgBin {
    foreach ($candidate in @(
        (Join-Path $Root '.pgsql\bin'),
        (Join-Path $Root '.pgsql\extracted\pgsql\bin')
    )) {
        if (Test-Path (Join-Path $candidate 'pg_ctl.exe')) { return $candidate }
    }
    if (Have 'pg_ctl') { return '' }
    return $null
}

function Ensure-Postgres {
    $bin = Resolve-PgBin
    if ($null -eq $bin) {
        Die @'
本机没有找到 PostgreSQL。
    方案 A：安装官方包 https://www.postgresql.org/download/windows/
    方案 B：把便携版 pgsql 解压到 .pgsql\extracted\pgsql\ 后重试
    方案 C：docker compose -f docker/docker-compose.yml --profile infra up -d postgres
'@
    }
    if (Test-Port '127.0.0.1' $PgPort) {
        Ok "PostgreSQL 已在 127.0.0.1:$PgPort 运行"
        return $bin
    }
    $pgctl = if ($bin) { Join-Path $bin 'pg_ctl.exe' } else { 'pg_ctl' }
    $dataDir = Join-Path $Root '.pgsql\data'
    if (-not (Test-Path $dataDir)) {
        $initdb = if ($bin) { Join-Path $bin 'initdb.exe' } else { 'initdb' }
        Info "初始化数据目录 $dataDir"
        & $initdb -D $dataDir -U $PgUser -E UTF8 --locale=C --auth=trust 2>&1 | ForEach-Object { Write-Log "  $_" }
    }
    Info '启动 PostgreSQL …'
    $log = Join-Path $LogDir 'postgres.log'
    Start-Process -FilePath $pgctl -ArgumentList @('-D', $dataDir, '-l', $log, '-w', 'start') -NoNewWindow | Out-Null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Port '127.0.0.1' $PgPort) { break }
    }
    if (-not (Test-Port '127.0.0.1' $PgPort)) { Die "PostgreSQL 未监听 $PgPort，见 $log" }
    Ok "PostgreSQL 已启动（127.0.0.1:$PgPort）"
    return $bin
}

function Ensure-Database([string]$bin) {
    $psql = if ($bin) { Join-Path $bin 'psql.exe' } else { 'psql' }
    if (-not (Test-Path $psql) -and -not (Have 'psql')) {
        Warn '找不到 psql，跳过建库；若启动时提示数据库不存在，请手动创建：CREATE DATABASE zhibiao;'
        return
    }
    $env:PGPASSWORD = $PgPassword
    $exists = (& $psql -h 127.0.0.1 -p $PgPort -U $PgUser -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DbName'" 2>&1 | Out-String)
    if ($exists -notmatch '1') {
        Info "创建数据库 $DbName …"
        & $psql -h 127.0.0.1 -p $PgPort -U $PgUser -d postgres -c "CREATE DATABASE `"$DbName`"" 2>&1 | ForEach-Object { Write-Log "  $_" }
    }
    Ok "数据库 $DbName 就绪"
}

function Install-Local {
    Step '1/7' '检查 Python'
    $py = if (Have 'python') { 'python' } elseif (Have 'py') { 'py' } else { $null }
    if (-not $py) { Die '未找到 Python 3.12+，请从 https://www.python.org/downloads/ 安装并勾选 Add to PATH' }
    $ver = (& $py -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>&1 | Out-String).Trim()
    if ([version]$ver -lt [version]'3.12') { Die "Python 版本过低：$ver（需要 3.12+）" }
    Ok "Python $ver"

    Step '2/7' '创建虚拟环境并安装后端依赖'
    $venvPy = Join-Path $Root 'venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
        & $py -m venv (Join-Path $Root 'venv') 2>&1 | ForEach-Object { Write-Log "  $_" }
    }
    if (-not (Test-Path $venvPy)) { Die '创建虚拟环境失败' }
    & $venvPy -m pip install --disable-pip-version-check -e $Root 2>&1 | ForEach-Object { Write-Log "  $_" }
    if ($LASTEXITCODE -ne 0) { Die 'pip install -e . 失败' }
    Ok '后端依赖安装完成'

    Step '3/7' '构建前端'
    if ($SkipFrontend) {
        Warn '已跳过前端构建（服务只提供 API）'
    } elseif (-not (Have 'npm')) {
        Warn '未找到 npm，跳过前端构建。安装 Node.js 后执行：cd packages\web; npm install; npm run build'
    } else {
        Push-Location (Join-Path $Root 'packages\web')
        if (-not (Test-Path 'node_modules')) { & npm install 2>&1 | ForEach-Object { Write-Log "  $_" } }
        & npm run build 2>&1 | ForEach-Object { Write-Log "  $_" }
        Pop-Location
        if (Test-Path (Join-Path $Root 'web_dist\index.html')) { Ok '前端构建完成：web_dist\index.html' }
        else { Die '前端构建失败，未找到 web_dist\index.html' }
    }

    Step '4/7' '准备 PostgreSQL'
    $bin = Ensure-Postgres

    Step '5/7' '建库 + 生成 .env'
    Ensure-Database $bin
    Write-RootEnv

    Step '6/7' '初始化数据表与管理员（首次启动时自动完成）'
    Info "管理员：$AdminUser / $AdminPassword"

    Step '7/7' '启动服务'
    if ($NoStart) {
        Info "手动启动：.\venv\Scripts\python.exe -m uvicorn zhibiao.app:app --port $ApiPort"
        return
    }
    if (Test-Port '127.0.0.1' $ApiPort) {
        Ok "端口 $ApiPort 已有服务在运行"
        return
    }
    Start-Process -FilePath $venvPy `
        -ArgumentList '-m', 'uvicorn', 'zhibiao.app:app', '--host', '0.0.0.0', '--port', "$ApiPort" `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'api.out.log') `
        -RedirectStandardError (Join-Path $LogDir 'api.err.log')
    $body = Wait-Http "http://127.0.0.1:$ApiPort/api/health" 90
    if ($body) { Ok 'API 已就绪' } else { Warn "API 未在 90 秒内就绪，见 $LogDir\api.err.log" }
}

function Install-Docker {
    Step '1/5' '检查 Docker 引擎'
    if (-not (Have 'docker')) { Warn '未找到 Docker Desktop'; return $false }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { Warn 'Docker 引擎不可用，请先启动 Docker Desktop'; return $false }
    Ok 'Docker 引擎可用'

    Step '2/5' '生成环境文件'
    Write-DockerEnv

    Step '3/5' '构建并启动'
    & docker compose -f docker/docker-compose.yml --env-file docker/.env.docker --profile infra up -d --build 2>&1 |
        ForEach-Object { Write-Log "  $_"; Write-Host "    $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) { Die 'docker compose up 失败' }
    Ok '容器已创建'

    Step '4/5' '等待健康检查'
    $body = Wait-Http "http://127.0.0.1:$ApiPort/api/health" 480
    if ($body) { Ok 'API 已就绪' } else { Warn 'API 未在 8 分钟内就绪，执行：docker compose -f docker/docker-compose.yml logs -f api' }

    Step '5/5' '完成'
    Info "管理员：$AdminUser / $AdminPassword（首次启动自动创建）"
    return $true
}

Title '智标 一键安装 / 部署'
Assert-Root

if (-not $AiApiKey -and -not $Yes) {
    Write-Host ''
    Write-Host '  提示：模型功能需要 AI API Key，可稍后在 .env 中补充。' -ForegroundColor Yellow
    $keyInput = Read-Host '  现在输入 API Key（直接回车跳过）'
    if ($keyInput) { $AiApiKey = $keyInput.Trim() }
}

if ($Mode -eq 'docker') {
    if (-not (Install-Docker)) { Die 'Docker 方式不可用，可改用：.\scripts\install.ps1 -Mode local' }
} elseif ($Mode -eq 'local') {
    Install-Local
} else {
    if (-not (Install-Docker)) {
        Warn '自动改用本地开发方式'
        Install-Local
    }
}

Title '完成'
Write-Host "  站点：      http://127.0.0.1:$ApiPort/"
Write-Host "  API 文档：  http://127.0.0.1:$ApiPort/docs"
Write-Host "  登录账号：  $AdminUser / $AdminPassword"
Write-Host "  日志：      $LogFile" -ForegroundColor DarkGray
