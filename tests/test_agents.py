from __future__ import annotations

import asyncio

from zhibiao.agents.audit import run_audit
from zhibiao.agents.base import AgentContext, AgentRunner, AgentSpec, ToolSpec
from zhibiao.agents.facts import normalize_facts
from zhibiao.models import BidSection, Tender


def make_tender() -> Tender:
    return Tender(
        id="tender",
        code="ZB-20261104-0001",
        title="智慧校园综合管理平台建设项目",
        buyer="某某大学",
        budget="420万元",
        deadline="2026-12-20",
    )


def make_section(body: str, position: int = 0) -> BidSection:
    return BidSection(
        id=f"section-{position}",
        document_id="doc",
        position=position,
        heading="技术方案" if position == 0 else "资格证明文件",
        target_words=120,
        body=body,
        status="written",
    )


def full_facts() -> dict:
    return normalize_facts(
        {
            "project": {
                "name": "智慧校园综合管理平台建设项目",
                "code": "ZB-20261104-0001",
                "buyer": "某某大学",
                "budget": "420万元",
                "deadline": "2026-12-20",
                "bidValidity": "90天",
                "duration": "120天",
                "deposit": "8万元",
            },
            "bidder": {"name": "某某科技有限公司"},
            "materials": [{"name": "投标函"}, {"name": "营业执照"}, {"name": "投标保证金回单"}],
            "parameters": [{"name": "统一身份认证并发数", "required": "不少于3000人"}],
            "qualifications": ["软件企业证书"],
            "commitments": ["数据迁移零丢失"],
            "pricing": {"maxPrice": "420万元", "currency": "人民币"},
            "template": {"modeHint": "generic", "chapters": ["投标函", "技术方案"]},
            "manualChecklist": ["检查封装袋封面正副本标记"],
        }
    )


def test_normalize_facts_keeps_lists():
    facts = full_facts()
    assert facts["project"]["name"] == "智慧校园综合管理平台建设项目"
    assert len(facts["materials"]) == 3
    assert len(facts["parameters"]) == 1
    assert facts["template"]["chapters"] == ["投标函", "技术方案"]


def test_audit_without_facts_is_blocker():
    tender = make_tender()
    report = run_audit(tender, None, [make_section("正文")], [])
    assert report["issues"][0]["severity"] == "blocker"
    assert "招标解析" in report["issues"][0]["message"]


def test_audit_finds_missing_material_and_parameter():
    tender = make_tender()
    facts = full_facts()
    section = make_section("我方将按照招标要求提交投标文件，工期 120 天，有效期 90 天。")
    report = run_audit(tender, facts, [section], ["投标函", "技术方案"])
    messages = " ".join(item["message"] for item in report["issues"])
    assert "投标函" in messages
    assert "统一身份认证并发数" in messages
    assert "模板章节未覆盖" in messages


def test_agent_runner_tool_loop(monkeypatch):
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def json_chat(self, _messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"thought": "调用工具", "action": "echo", "args": {"value": "ok"}}
            return {"thought": "完成", "action": "finish", "args": {"result": {"done": True}}}

    async def fake_client():
        return FakeClient()

    async def echo(_context, args):
        return {"echo": args}

    monkeypatch.setattr("zhibiao.agents.base.get_chat_client", fake_client)
    spec = AgentSpec(
        key="fake",
        name="测试 Agent",
        description="测试工具循环",
        system_prompt="按 JSON 输出。",
        tools=[ToolSpec("echo", "回显参数", echo)],
        max_steps=3,
    )
    context = AgentContext(tender_id="t", job_id="j", account="a")
    outcome = asyncio.run(AgentRunner().run(spec, context, "执行测试", {}))
    assert outcome["output"]["done"] is True
    assert [step["action"] for step in outcome["trace"]] == ["echo", "finish"]


def test_audit_passes_when_document_matches_facts():
    tender = make_tender()
    facts = full_facts()
    body = (
        "智慧校园综合管理平台建设项目 ZB-20261104-0001 投标人：某某科技有限公司。"
        "本项目提交投标函、营业执照、投标保证金回单。统一身份认证并发数不少于3000人。"
        "我方具备软件企业证书，工期120天，投标有效期90天，投标保证金8万元，承诺数据迁移零丢失。"
        "报价不超过420万元。"
    )
    sections = [make_section(body + "技术方案", 0), make_section(body + "资格证明文件", 1)]
    report = run_audit(tender, facts, sections, ["技术方案", "资格证明文件"])
    severe = [item for item in report["issues"] if item["severity"] in {"blocker", "critical", "major"}]
    assert not severe
    assert any(item["category"] == "人工核对" for item in report["issues"])
