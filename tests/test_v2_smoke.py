from __future__ import annotations

import os
import tempfile
import time
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

    capabilities = client.get("/api/admin/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["ocr"]["enabled"] is False
    assert capabilities.json()["image"]["enabled"] is False
    assert capabilities.json()["image"]["maxPerBook"] == 8
    saved = client.put(
        "/api/admin/capabilities",
        json={
            "ocr": {"enabled": True, "baseUrl": "https://example.invalid/v1", "maxPages": 3},
            "image": {"enabled": True, "baseUrl": "https://example.invalid/v1"},
        },
    )
    assert saved.status_code == 200
    assert saved.json()["ocr"]["ready"] is True
    assert saved.json()["image"]["ready"] is True
    assert saved.json()["ocr"]["apiKeySet"] is False
    off = client.put(
        "/api/admin/capabilities",
        json={"ocr": {"enabled": False}, "image": {"enabled": False}},
    )
    assert off.status_code == 200
    assert off.json()["ocr"]["enabled"] is False
    assert off.json()["image"]["enabled"] is False

    agents = client.get("/api/agents")
    assert agents.status_code == 200
    assert {item["key"] for item in agents.json()["items"]} >= {
        "tender_reader",
        "planner",
        "builder",
        "auditor",
        "fixer",
        "template",
    }

    deleted = client.delete(f"/api/tenders/{tender_id}")
    assert deleted.status_code == 200


def test_outline_editing_and_batch_generation(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    login = client.post("/api/auth/login", json={"username": "admin", "password": "sxit@123456"})
    assert login.status_code == 200
    created = client.post("/api/tenders", json={"title": "批量生成测试"})
    tender_id = created.json()["id"]

    outline = client.post(
        f"/api/tenders/{tender_id}/outline",
        json={"mode": "standard", "targetWords": 50000},
    )
    assert outline.status_code == 200
    sections = outline.json()["sections"]
    assert len(sections) >= 20
    assert all(section["targetWords"] > 0 for section in sections)

    added = client.post(
        f"/api/tenders/{tender_id}/sections",
        json={"heading": "补充承诺", "targetWords": 800},
    )
    assert added.status_code == 200
    section_id = added.json()["id"]
    moved = client.post(
        f"/api/tenders/{tender_id}/sections/{section_id}/move",
        json={"direction": "up"},
    )
    assert moved.status_code == 200
    assert client.delete(f"/api/tenders/{tender_id}/sections/{section_id}").status_code == 200

    async def fake_generate(_client, _tender, _analysis, section, _sections):
        return f"## {section.heading}\n" + ("本节按招标要求给出可核查的实施安排与量化承诺。" * 12)

    monkeypatch.setattr("zhibiao.generation.runner.generate_body", fake_generate)
    started = client.post(f"/api/tenders/{tender_id}/generation", json={"mode": "missing"})
    assert started.status_code == 200
    job_id = started.json()["id"]

    job = None
    for _ in range(80):
        time.sleep(0.15)
        state = client.get(f"/api/generation/{job_id}")
        if state.status_code == 200:
            job = state.json()
            if job["status"] not in {"queued", "running"}:
                break
    assert job is not None
    assert job["status"] == "succeeded"
    assert job["result"]["generated"] >= 20

    document = client.get(f"/api/tenders/{tender_id}/document")
    assert document.status_code == 200
    assert all(section["body"].strip() for section in document.json()["sections"])

    report = client.post(f"/api/tenders/{tender_id}/generation/postprocess")
    assert report.status_code == 200
    assert "stats" in report.json()

    assert client.delete(f"/api/tenders/{tender_id}").status_code == 200
