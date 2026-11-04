from __future__ import annotations

import re
from collections import Counter

from zhibiao.generation.quality import BANNED_PHRASES, completeness
from zhibiao.models import BidSection, Tender

MONEY_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(亿元|万元|元)")
NUMBER_LABEL = re.compile(
    r"(投标有效期|有效期|总工期|工期|建设周期|质保期|保修期|投标保证金|保证金)"
    r"[^。；\n]{0,30}?(\d+(?:\.\d+)?)\s*(天|日|个月|月|年|万元|元)"
)
BIDDER_NAME = re.compile(r"投标人(?:名称)?[：:]\s*([^\n，,。；;]{2,40})")


def _issue(
    category: str,
    severity: str,
    message: str,
    section: BidSection | None = None,
    evidence: str = "",
) -> dict:
    return {
        "sectionId": section.id if section else None,
        "heading": section.heading if section else "全文",
        "severity": severity,
        "category": category,
        "message": message,
        "evidence": evidence[:300],
    }


def _find_section(sections: list[BidSection], keyword: str) -> BidSection | None:
    clean = re.sub(r"\s+", "", keyword)
    for section in sections:
        if clean and clean in re.sub(r"\s+", "", section.heading + (section.body or "")):
            return section
    return None


def _money_values(text: str) -> list[float]:
    values = []
    for match in MONEY_PATTERN.finditer(text):
        number = float(match.group(1))
        unit = match.group(2)
        if unit == "亿元":
            number *= 10000
        elif unit == "元":
            number /= 10000
        values.append(number)
    return values


def _number_in(text: str, value: str) -> bool:
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    target = re.findall(r"\d+(?:\.\d+)?", value)
    return bool(target) and all(item in numbers for item in target)


def run_audit(
    tender: Tender,
    facts: dict | None,
    sections: list[BidSection],
    template_headings: list[str] | None = None,
) -> dict:
    issues: list[dict] = []
    doc_text = "\n".join(f"{section.heading}\n{section.body or ''}" for section in sections)
    headings = [section.heading for section in sections]

    if not facts:
        issues.append(
            _issue(
                "事实来源",
                "blocker",
                "尚未从招标文件抽取核对事实，请先运行「招标解析 Agent」",
            )
        )
        return {"issues": issues, "stats": {"checks": 1, "issues": len(issues), "manual": 0}}

    project = facts.get("project") or {}
    name = str(project.get("name") or "").strip()
    code = str(project.get("code") or "").strip()
    if name and name not in doc_text:
        issues.append(_issue("项目身份", "blocker", f"成稿中未找到项目名称「{name}」", evidence=name))
    if code and code not in doc_text:
        issues.append(_issue("项目身份", "major", f"成稿中未找到项目编号「{code}」", evidence=code))

    bidder = facts.get("bidder") or {}
    bidder_name = str(bidder.get("name") or "").strip()
    if bidder_name:
        if bidder_name not in doc_text:
            issues.append(
                _issue("投标人身份", "blocker", f"成稿中未找到投标人名称「{bidder_name}」", evidence=bidder_name)
            )
        found_names = Counter(match.group(1).strip() for match in BIDDER_NAME.finditer(doc_text))
        for other, count in found_names.items():
            if other and bidder_name not in other and other not in bidder_name and count >= 1:
                issues.append(
                    _issue(
                        "投标人身份",
                        "major",
                        f"成稿出现不一致的投标人名称「{other}」（{count} 处）",
                        evidence=other,
                    )
                )

    pricing = facts.get("pricing") or {}
    max_price = str(pricing.get("maxPrice") or project.get("budget") or "")
    max_value = None
    if max_price:
        values = _money_values(max_price)
        max_value = values[0] if values else None
    if max_value:
        over = [value for value in _money_values(doc_text) if value > max_value * 1.02]
        if over:
            issues.append(
                _issue(
                    "报价",
                    "critical",
                    f"成稿出现高于最高限价的报价：{max(over):g} 万元（限价约 {max_value:g} 万元）",
                    evidence=max_price,
                )
            )

    for label, value in (
        ("投标有效期", project.get("bidValidity")),
        ("总工期", project.get("duration")),
        ("投标保证金", project.get("deposit")),
    ):
        if not value:
            continue
        if not _number_in(doc_text, str(value)):
            issues.append(
                _issue("期限与金额", "major", f"成稿中未找到与招标文件一致的「{label}」：{value}", evidence=str(value))
            )

    for item in facts.get("materials") or []:
        keyword = str(item.get("name") if isinstance(item, dict) else item).strip()
        if keyword and keyword not in doc_text:
            section = _find_section(sections, "资格")
            issues.append(
                _issue("材料完整性", "major", f"缺少招标文件要求的材料/文件：{keyword}", section, keyword)
            )

    for item in facts.get("parameters") or []:
        keyword = str(item.get("name") if isinstance(item, dict) else item).strip()
        if not keyword:
            continue
        if keyword not in doc_text:
            issues.append(
                _issue("参数响应", "major", f"技术参数未响应：{keyword}", _find_section(sections, "技术"), keyword)
            )
        required = str(item.get("required") or "").strip() if isinstance(item, dict) else ""
        if required:
            numbers = re.findall(r"\d+(?:\.\d+)?", required)
            if numbers and not _number_in(doc_text, required):
                issues.append(
                    _issue(
                        "参数响应",
                        "major",
                        f"参数「{keyword}」的数值未按招标文件响应：{required}",
                        _find_section(sections, "技术"),
                        required,
                    )
                )

    for label, items in (("资格要求", facts.get("qualifications")), ("实质性承诺", facts.get("commitments"))):
        for item in items or []:
            keyword = re.sub(r"\s+", "", str(item))[:24]
            if keyword and keyword not in re.sub(r"\s+", "", doc_text):
                issues.append(
                    _issue("响应性", "major", f"{label}未在成稿中体现：{item}", _find_section(sections, "资格"), str(item))
                )

    if template_headings:
        missing = []
        normalized = {re.sub(r"\s+", "", heading) for heading in headings}
        for expected in template_headings:
            key = re.sub(r"\s+", "", expected)
            if key and key not in normalized:
                missing.append(expected)
        if missing:
            issues.append(
                _issue(
                    "模板覆盖",
                    "major",
                    f"招标文件模板章节未覆盖：{'、'.join(missing[:8])}",
                    evidence="、".join(missing[:8]),
                )
            )

    issues.extend(completeness(sections))
    for section in sections:
        for phrase, severity in BANNED_PHRASES:
            if phrase in (section.body or ""):
                issues.append(
                    _issue("表达", severity, f"出现推诿或占位表述「{phrase}」", section, phrase)
                )

    manual = []
    signature = facts.get("signature") or {}
    copies = facts.get("copies") or {}
    for label, value in (("签字盖章", signature), ("正副本份数", copies)):
        if value:
            manual.append(f"{label}：{value}")
    for item in facts.get("manualChecklist") or []:
        manual.append(str(item))
    if manual:
        issues.append(
            _issue(
                "人工核对",
                "info",
                "以下内容无法仅凭文本自动判定，需人工核对：" + "；".join(manual[:10]),
            )
        )

    return {
        "issues": issues,
        "stats": {
            "checks": 12,
            "issues": len(issues),
            "blockers": sum(1 for item in issues if item["severity"] == "blocker"),
            "manual": len(manual),
        },
    }
