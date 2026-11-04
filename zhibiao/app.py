from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from zhibiao.api import router
from zhibiao.database import close_database, database_ready, open_database
from zhibiao.seed import seed_all
from zhibiao.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    await open_database()
    if database_ready():
        await seed_all()
    yield
    await close_database()


class SpaFiles(StaticFiles):
    """静态站点：缺失的页面回退到 index.html，资源文件缺失时保持 404。"""

    async def get_response(self, path: str, scope):
        relative = path.replace("\\", "/").lstrip("/")
        if relative == "api" or relative.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        asset = relative.startswith("assets/")
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or asset:
                raise
            response = await super().get_response("index.html", scope)
        else:
            if response.status_code == 404 and not asset:
                response = await super().get_response("index.html", scope)
        if asset:
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=f"{settings.app_name} API", version=settings.app_version, lifespan=lifespan)

    origins = [item.strip() for item in settings.allowed_origins.split(",") if item.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(router)

    site = settings.web_dist_path()
    if site:
        app.mount("/", SpaFiles(directory=str(site), html=True), name="web")
    return app


app = create_app()
