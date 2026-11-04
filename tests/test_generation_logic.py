from __future__ import annotations

from zhibiao.generation.outline import assign_targets, match_scores, standard_plan, tender_plan
from zhibiao.generation.quality import run_quality
from zhibiao.generation.writer import cap_tokens, finalize, merge_overlap, needs_continue
from zhibiao.models import BidSection


def make_section(heading: str, body: str, target: int = 800, position: int = 0) -> BidSection:
    return BidSection(
        id=f"section-{position}",
        document_id="doc",
        position=position,
        heading=heading,
        target_words=target,
        body=body,
    )


def test_standard_outline_and_targets():
    plans = standard_plan()
    assert len(plans) >= 20
    assert any(plan.level == 1 for plan in plans)
    scores = [
        {"name": "技术方案", "points": 35, "criteria": "总体架构、数据治理、安全设计"},
        {"name": "类似业绩", "points": 10, "criteria": "同类项目合同"},
        {"name": "售后服务", "points": 10, "criteria": "运维方案与响应时效"},
    ]
    match_scores(plans, scores)
    assert any("技术方案" in plan.scores for plan in plans if plan.heading == "技术方案")
    assign_targets(plans, 80000)
    assert all(300 <= plan.target_words <= 8000 for plan in plans)
    assert sum(plan.target_words for plan in plans) <= 80000


def test_tender_outline_parsing():
    corpus = """第六章 投标文件格式
一、投标函
二、法定代表人身份证明
三、商务响应
四、技术方案
五、售后服务
"""
    plans = tender_plan(corpus)
    assert len(plans) >= 3
    assert any("投标函" in plan.heading for plan in plans)


def test_generation_quality_dedup_and_conflicts():
    shared = "本项目采用统一的数据治理规范，所有接口遵循同一套安全基线，并在实施阶段完成等保整改。" * 2
    first = make_section("技术方案", f"## 总体架构\n{shared}\n\n建设工期为 120 天。", 1200, 0)
    second = make_section(
        "实施方案",
        f"## 实施安排\n{shared}\n\n建设工期为 90 天。详见招标文件。",
        1200,
        1,
    )
    report = run_quality([first, second])
    assert second.id in report["updates"]
    categories = {issue["category"] for issue in report["issues"]}
    assert "重复" in categories
    assert "一致性" in categories
    assert "表达" in categories
    assert "完整性" in categories


def test_writer_length_helpers():
    assert cap_tokens(800) >= 700
    text = "本节内容如下：" * 120
    assert needs_continue(text, 2000)
    merged = merge_overlap("前文内容", "内容后文")
    assert "内容" in merged
    final = finalize("# 章节标题\n\n正文内容。", "章节标题", 800)
    assert not final.startswith("#")
