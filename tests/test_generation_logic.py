from __future__ import annotations

from zhibiao.generation.outline import (
    SectionPlan,
    assign_targets,
    expand_template_scores,
    match_scores,
    standard_plan,
    tender_plan,
)
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


def test_tender_outline_skips_document_list_reference():
    """投标人须知里的「（6）投标文件格式；」不是格式章，不能截断目录抽取。

    线上事故：第一个「投标文件格式」字样出现在「招标文件的组成」清单里，
    旧实现从这里只扫 300 行，真正的第六章在几千字之后，抽不到就回退成标准
    结构，还把提示写成「已按原件目录生成」。
    """
    filler = "\n".join(f"第 {index} 条 投标人须知条款，约定投标文件的编制与递交要求。" for index in range(320))
    corpus = f"""第六章 投标文件格式
第二章 投标人须知
2.1 招标文件的组成
（6）投标文件格式；
{filler}
第六章 投标文件格式
目 录
一、投标函
二、法定代表人身份证明
三、投标保证金
四、商务和技术偏差表
五、分项报价表
六、资格审查资料
"""
    plans = tender_plan(corpus)
    top = [plan.heading for plan in plans if plan.level == 1]
    assert top == ["投标函", "法定代表人身份证明", "投标保证金", "商务和技术偏差表", "分项报价表", "资格审查资料"]
    assert "商务响应" not in top


def test_price_variables_do_not_invent_rate():
    from zhibiao.core.format_subsections import price_variables

    letter = """（招标人名称）：
1．我方已仔细研究了 （项目名称）设备采购招标项目招标文件的全部内容，愿意以人民币（大写）
（¥ ）的投标总报价（含税）提供 （设备名称及技术服务和质保期服务），并按合同约定履行义务。
"""
    labels = price_variables(letter)
    assert labels[0] == "投标总报价（含税）"
    assert all("率" not in label for label in labels[1:])

    assert price_variables(letter + "\n下浮率（%）：") == ["投标总报价（含税）", "下浮率（%）"]


def test_score_item_follows_its_parent():
    """评分项追加章节必须落在父章子章之后，不能挂到最后一个一级章下。"""
    plans = [
        SectionPlan(heading="投标函", level=1),
        SectionPlan(heading="技术服务和质保期服务计划", level=1),
        SectionPlan(heading="供货与交付计划", level=2),
        SectionPlan(heading="其他资料", level=1),
    ]
    scores = [{"name": "技术服务和质保期服务能力", "points": 10, "criteria": "服务保障"}]
    result = expand_template_scores(plans, scores)
    headings = [plan.heading for plan in result]
    added = result[headings.index("技术服务和质保期服务能力")]
    assert added.level == 2
    assert headings.index("技术服务和质保期服务能力") < headings.index("其他资料")


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
