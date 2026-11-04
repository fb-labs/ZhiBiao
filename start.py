#!/usr/bin/env python3
"""
智标 快速启动脚本
检查环境依赖、初始化数据表与管理员账号，然后启动服务。
"""

import asyncio
import importlib.util
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent


def check_python_version() -> bool:
    version = sys.version_info
    if version.major < 3 or (version.major == 3 and version.minor < 12):
        print(f"[X] Python 版本过低: {version.major}.{version.minor}")
        print("   需要 Python 3.12+（见 pyproject.toml 的 requires-python）")
        return False
    print(f"[OK] Python 版本: {version.major}.{version.minor}.{version.micro}")
    return True


def check_env_file() -> bool:
    env_path = ROOT / ".env"
    if not env_path.exists():
        print("[!] .env 文件不存在")
        example_path = ROOT / ".env.example"
        if example_path.exists():
            import shutil

            shutil.copy(example_path, env_path)
            print("[OK] 已从 .env.example 创建 .env，请填写模型 API Key")
        else:
            print("[X] .env.example 也不存在")
        return False
    print("[OK] .env 文件存在")
    return True


def check_dependencies() -> bool:
    required = [
        ("fastapi", "FastAPI"),
        ("uvicorn", "Uvicorn"),
        ("sqlalchemy", "SQLAlchemy"),
        ("pydantic", "Pydantic"),
        ("openai", "OpenAI SDK"),
        ("bcrypt", "bcrypt"),
        ("docx", "python-docx"),
        ("reportlab", "reportlab"),
        ("pdfplumber", "pdfplumber"),
    ]

    missing = []
    for module, name in required:
        if importlib.util.find_spec(module) is None:
            missing.append(name)
            print(f"[X] 缺少依赖: {name}")
        else:
            print(f"[OK] 依赖已安装: {name}")
    return len(missing) == 0


def install_dependencies() -> bool:
    print("\n[*] 正在安装依赖...")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-e", ".[dev]"], cwd=ROOT)
    return result.returncode == 0


def check_database() -> bool:
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from zhibiao.settings import get_settings

    settings = get_settings()
    dsn = settings.database_dsn()
    shown = make_url(dsn).render_as_string(hide_password=True)

    async def probe() -> None:
        engine = create_async_engine(dsn)
        try:
            async with asyncio.timeout(6):
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 数据库连接失败（{settings.db_type}: {shown}）: {exc}")
        print("   可改用 SQLite 快速体验：")
        print("     ZHIBIAO_DB_TYPE=sqlite")
        print("     ZHIBIAO_DATABASE_URL=sqlite+aiosqlite:///./zhibiao.db")
        print("   或启动本地 PostgreSQL：python start.py 会自动尝试")
        return False

    print(f"[OK] 数据库连接成功（{settings.db_type}）")
    return True


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def _pg_bin_dir() -> Path | None:
    exe = "pg_ctl.exe" if sys.platform == "win32" else "pg_ctl"
    candidates: list[Path] = []
    try:
        import embedded_postgres

        candidates.append(Path(embedded_postgres.__file__).resolve().parent / "pginstall" / "bin")
    except Exception:  # noqa: BLE001
        pass
    candidates.append(ROOT / ".pgsql" / "bin")
    candidates.append(ROOT / ".pgsql" / "extracted" / "pgsql" / "bin")
    for candidate in candidates:
        if (candidate / exe).is_file():
            return candidate
    return None


def ensure_database() -> bool:
    """数据库不可用时自动启动本地 PostgreSQL。"""
    from sqlalchemy.engine import make_url

    from zhibiao.settings import get_settings

    settings = get_settings()
    if settings.db_type == "sqlite":
        print("[..] 当前使用 SQLite，无需启动数据库服务")
        return True
    if settings.db_type == "mysql":
        print("[!] 当前使用 MySQL，请先启动 MySQL 服务（管理员执行：net start MySQL80）")
        return False

    url = make_url(settings.database_dsn())
    host = url.host or "localhost"
    port = url.port or 5432
    if _port_open(host, port):
        print(f"[OK] PostgreSQL 已在 {host}:{port} 运行")
        return True

    bin_dir = _pg_bin_dir()
    if not bin_dir:
        print("[X] 未找到 pg_ctl，无法自动启动 PostgreSQL。")
        print("   安装嵌入式 PostgreSQL：pip install embedded-postgres")
        print("   或使用 Docker：docker compose -f docker/docker-compose.yml --profile infra up -d postgres")
        return False

    pg_ctl = bin_dir / ("pg_ctl.exe" if sys.platform == "win32" else "pg_ctl")
    initdb = bin_dir / ("initdb.exe" if sys.platform == "win32" else "initdb")
    data_dir = ROOT / ".pgsql" / "data"
    data_dir.parent.mkdir(parents=True, exist_ok=True)

    if not (data_dir / "PG_VERSION").exists():
        print(f"[*] 初始化本地 PostgreSQL 数据目录：{data_dir}")
        result = subprocess.run(
            [str(initdb), "-D", str(data_dir), "-U", "postgres", "-E", "UTF8", "--locale=C", "--auth=trust"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("[X] initdb 失败：\n" + (result.stderr or result.stdout).strip()[-600:])
            return False
        print("[OK] 数据目录初始化完成")

    log_file = ROOT / ".pgsql" / "pg.log"
    print(f"[*] 启动 PostgreSQL：{pg_ctl} -D {data_dir}")
    result = subprocess.run(
        [str(pg_ctl), "-D", str(data_dir), "-l", str(log_file), "-w", "-t", "30", "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0 and not _port_open(host, port):
        tail = ""
        try:
            tail = log_file.read_text(encoding="utf-8", errors="ignore").strip()[-600:]
        except OSError:
            pass
        print("[X] PostgreSQL 启动失败：" + (tail or f"pg_ctl 返回 {result.returncode}"))
        print(f"   日志：{log_file}")
        return False

    deadline = time.time() + 20
    while time.time() < deadline:
        if _port_open(host, port):
            print(f"[OK] PostgreSQL 已启动（{host}:{port}）")
            return True
        time.sleep(1)
    print(f"[!] PostgreSQL 未在预期时间内监听 {host}:{port}，请查看 {log_file}")
    return False


def prepare_database() -> None:
    from zhibiao.settings import get_settings

    print("\n[*] 检查/创建数据表与初始账号...")
    get_settings().ensure_dirs()
    try:
        asyncio.run(_prepare())
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 数据表初始化失败：{exc}")
        return
    print("[OK] 数据表与管理员账号已就绪")


async def _prepare() -> None:
    from zhibiao.database import close_database, open_database
    from zhibiao.seed import seed_all

    await open_database()
    await seed_all()
    await close_database()


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start_server() -> None:
    from zhibiao.settings import get_settings

    settings = get_settings()
    port = settings.port
    if port_in_use(port):
        print(f"[X] 端口 {port} 已被占用，服务可能已经在运行。")
        print(f"   可直接访问 http://localhost:{port}/ ，或先停止占用进程后重试。")
        sys.exit(1)

    print("\n[*] 启动 FastAPI 服务...")
    print("=" * 50)
    print(f"站点地址: http://localhost:{port}  （页面与 API 同端口）")
    print(f"API 文档: http://localhost:{port}/docs")
    print("=" * 50)
    print("\n按 Ctrl+C 停止服务\n")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "zhibiao.app:app",
            "--host",
            settings.host,
            "--port",
            str(port),
            "--reload",
        ],
        cwd=ROOT,
    )


def main() -> None:
    print("=" * 50)
    print("  智标 启动检查")
    print("=" * 50)
    print()

    if not check_python_version():
        sys.exit(1)

    print()
    check_env_file()
    print()

    if not check_dependencies():
        print("\n是否安装缺失的依赖? (y/n): ", end="")
        if input().lower() == "y":
            if not install_dependencies():
                print("[X] 依赖安装失败")
                sys.exit(1)
        else:
            sys.exit(1)

    print()
    db_ready = check_database()
    if not db_ready:
        print()
        print("[*] 尝试自动启动本地数据库服务...")
        if ensure_database():
            db_ready = check_database()
    if db_ready:
        prepare_database()

    print()
    print("=" * 50)
    print("  环境检查完成")
    print("=" * 50)
    print()

    if not db_ready:
        print("[!] 数据库不可用，启动后业务接口会返回 503。")
        print("    是否仍以降级模式启动？(y/n): ", end="")
        if input().lower() != "y":
            print("已退出。请先修改 .env 中的数据库配置。")
            sys.exit(1)

    start_server()


if __name__ == "__main__":
    main()
