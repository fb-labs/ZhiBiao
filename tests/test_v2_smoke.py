from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

ROOT = Path(tempfile.mkdtemp(prefix="zhibiao-test-"))
os.environ["ZHIBIAO_DB_TYPE"] = "sqlite"
os.environ["ZHIBIAO_DATABASE_URL"] = f"sqlite+aiosqlite:///{ROOT / 'test.db'}"
os.environ["ZHIBIAO_DATA_ROOT"] = str(ROOT / "var")
os.environ["ZHIBIAO_UPLOAD_ROOT"] = str(ROOT / "var" / "uploads")
os.environ["ZHIBIAO_DELIVERY_ROOT"] = str(ROOT / "var" / "deliveries")
os.environ["ZHIBIAO_TEMPLATE_ROOT"] = str(ROOT / "var" / "templates")
os.environ["ZHIBIAO_AI_API_KEY"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from zhibiao.app import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["dbReady"] is True


def test_requires_login(client: TestClient):
    assert client.get("/api/tenders").status_code == 401


def test_login_and_workflow(client: TestClient):
    login = client.post("/api/auth/login", json={"username": "admin", "password": "sxit@123456"})
    assert login.status_code == 200
    account = login.json()["account"]
    assert account["username"] == "admin"
    assert "admin.ai" in account["permissions"]

    created = client.post("/api/tenders", json={"title": "测试投标项目", "buyer": "测试采购人"})
    assert created.status_code == 200
    tender_id = created.json()["id"]

    uploaded = client.post(
        f"/api/tenders/{tender_id}/files",
        files={"file": ("招标文件.txt", "这是招标文件正文。".encode("utf-8"), "text/plain")},
        data={"role": "tender"},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["parseState"] == "parsed"

    detail = client.get(f"/api/tenders/{tender_id}").json()
    assert detail["fileCount"] == 1

    analysis = client.post(f"/api/tenders/{tender_id}/analysis")
    assert analysis.status_code == 400

    outline = client.post(f"/api/tenders/{tender_id}/outline")
    assert outline.status_code == 400

    review = client.post(f"/api/tenders/{tender_id}/reviews", json={"mode": "rule"})
    assert review.status_code == 400

    overview = client.get("/api/stats/overview")
    assert overview.status_code == 200
    assert overview.json()["total"] >= 1

    templates = client.get("/api/templates")
    assert templates.status_code == 200
    assert templates.json()["items"]

    accounts = client.get("/api/admin/accounts")
    assert accounts.status_code == 200
    assert accounts.json()["items"][0]["username"] == "admin"

    roles = client.get("/api/admin/roles")
    assert roles.status_code == 200
    assert {item["code"] for item in roles.json()["items"]} >= {"admin", "editor"}

    deleted = client.delete(f"/api/tenders/{tender_id}")
    assert deleted.status_code == 200
