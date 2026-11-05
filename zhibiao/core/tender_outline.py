"""按招标文件自身格式生成大纲（动态模板 `tender`）。

为什么要有它：内置模板（general / technical …）是通用骨架，换一份招标文件就得人工改
一遍；而**招标文件里规定投标文件格式的那一章**（第六、第七章都可能）本来就规定了投标
文件要有哪些部分，供货/技术要求那一章又规定了技术要求怎么分。这两处是「这份招标文件
专属」的，必须按文件解析出来，而不是再写一套固定章节，也不是假定章号。

输出口径：
- 格式章给出的每个部分 = 一级章节（一 投标函 / 九 投标设备技术性能指标的详细描述 …）；
- 技术类部分（九/十/十一 …）下面按招标文件供货要求章的小节与固定服务项展开二级章节；
- 抽不到格式章时，退回「供货要求章小节 + 技术服务」的骨架，仍然是这份文件专属的。

纯字符串解析，不调用大模型。
"""
from __future__ import annotations

import re
from typing import Any

from zhibiao.core.completeness import tech_items
from zhibiao.core.tender_checkup import (format_chapter, official_structure,
                                     section_headings, tech_chapter)
from zhibiao.core.text_hygiene import fix_quote_pairing

_CN_NUM = "一二三四五六七八九十"
_HEADING = re.compile(r"^[ \t]*([%s]{1,3})[、.．][ \t]*([^0-9。，,；;：:]{2,24})[ \t]*$" % _CN_NUM)
_CHAPTER = re.compile(r"^[ \t]*第([一二三四五六七八九十\d]{1,3})章")

# 技术类部分的识别关键词（招标文件写法各异，按语义认，不认固定章号）
_TECH_PART = re.compile(r"技术性能指标|技术指标|技术方案|技术要求|技术规格|技术响应|技术资料|技术支持")
_SERVICE_PART = re.compile(r"技术服务|质保|售后服务|服务计划|培训")

_SUB_TECH = ("技术参数逐条响应对照表", "主要结构与关键配置", "性能指标与裕度说明", "供货范围与接口界面")
_SUB_SUPPORT = ("设计图纸与计算书", "检测与试验报告", "供货业绩与用户证明")
_SUB_SERVICE = ("供货与交付计划", "安装调试与试运行", "培训计划", "售后服务响应时限", "质保期承诺与备件供应")


def _clean_title(title: str) -> str:
    """去掉「投标设备技术性能指标的详细描述」这类冗长修饰里的括号说明。

    招标文件里常见两种写法：括号闭合的（适用于无委托代理），以及被截断的
    （少一个右括号，如「法定代表人身份证明（适用于无委托代」）。
    后者必须从落单的左括号处截断，否则会生成半个括号的章节名。

    标题引号也要配平：「8.1 零整比”」缺左引号，成稿目录里就是半个引号。
    """
    cleaned = re.sub(r"[（(][^）)]{0,24}[）)]", "", str(title or ""))
    cleaned = re.split(r"[（(]", cleaned)[0]
    cleaned = fix_quote_pairing(cleaned)
    return cleaned.strip(" 　\t、，。：:《》")


def outline_from_tender(text: str) -> tuple[list[tuple[int, str]], str]:
    """返回 (章节列表, 说明)。章节为空表示这份文件抽不出可用骨架。"""
    source = str(text or "")
    if not source.strip():
        return [], "招标文件正文为空，无法按招标文件格式建目录"
    official = official_structure(source)
    # 评标办法/投标人须知里的流程条款（（四）投标、（五）开标、（六）评标、（七）授予合同）
    # 不是投标文件要写的章节。实测事故：兰花科创那份 PDF 没有独立的「投标文件格式」章，
    # 结果把开标、评标、授予合同当成了投标文件章节，大纲全错。
    _FLOW = re.compile(r"开标|评标|授予合同|定标|中标通知|签订合同|质疑|投诉|废标|重新招标|投标人须知")
    official = [x for x in official if not _FLOW.search(x.split(" ", 1)[-1])]
    if len(official) < 2:
        official = []
    format_span = format_chapter(source)
    tech_span = tech_chapter(source)
    format_label = ("%s「%s」" % (format_span["label"], format_span["title"])) if format_span else ""
    tech_label = ("%s「%s」" % (tech_span["label"], tech_span["title"])) if tech_span else ""
    sections = [t for t in section_headings(source) if 2 <= len(t) <= 24]
    items = tech_items(source)

    chapters: list[tuple[int, str]] = []
    seen: set[str] = set()

    def add(level: int, title: str) -> None:
        clean = _clean_title(title)
        if not clean or clean in seen:
            return
        seen.add(clean)
        chapters.append((level, clean))

    if official:
        tech_part_seen = service_part_seen = False
        for label in official:
            title = label.split(" ", 1)[-1]
            add(1, title)
            if _TECH_PART.search(title) and not tech_part_seen:
                tech_part_seen = True
                for section in sections[:6]:
                    add(2, section)
                for sub in _SUB_TECH:
                    add(2, sub)
            elif _SERVICE_PART.search(title) and not service_part_seen:
                service_part_seen = True
                for sub in _SUB_SERVICE:
                    add(2, sub)
            elif re.search(r"技术资料|技术支持", title):
                for sub in _SUB_SUPPORT:
                    add(2, sub)
    else:
        # 没有格式章：给一份完整的投标文件骨架（商务/报价/资格/技术），
        # 技术章节用这份文件自己的技术要求小节，仍然是文件专属的。
        add(1, "商务部分")
        for sub in ("投标函", "法定代表人身份证明", "授权委托书", "投标保证金",
                    "商务和技术偏差表", "投标人基本情况"):
            add(2, sub)
        add(1, "报价部分")
        for sub in ("开标一览表", "分项报价表", "备品备件与易损件报价"):
            add(2, sub)
        add(1, "资格证明文件")
        for sub in ("营业执照及资质证书", "近三年同类业绩", "财务状况报告",
                    "依法缴纳税收和社会保障资金的证明", "无重大违法记录声明"):
            add(2, sub)
        add(1, "技术部分")
        for section in sections[:8]:
            add(2, section)
        if items:
            add(2, "技术参数逐条响应对照")
        for sub in _SUB_SERVICE:
            add(2, sub)

    if not chapters:
        return [], "未从招标文件抽出章节骨架"
    if official and format_label:
        note = "按招标文件%s（%d 部分）" % (format_label, len(official))
        note += ("与%s小节生成" % tech_label) if tech_label and sections else "生成"
    elif sections:
        note = "未找到投标文件格式章，按%s %d 个小节生成" % (tech_label or "供货/技术要求章", len(sections))
    else:
        note = "未找到投标文件格式章与技术要求小节，只能给出通用技术服务骨架"
    return chapters, note


def tender_template_meta(chapters: list[tuple[int, str]], note: str) -> dict[str, Any]:
    return {
        "name": "按招标文件格式（自动）",
        "desc": note,
        "chapters": list(chapters),
        "dynamic": True,
        "source": "招标文件的投标文件格式章 + 供货要求章小节（按文件解析，章号不固定）",
    }