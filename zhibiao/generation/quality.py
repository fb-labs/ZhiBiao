from __future__ import annotations

import re
from collections import defaultdict

from zhibiao.models import BidSection

BANNED_PHRASES = (
    ("详见招标文件", "major"),
    ("以招标文件为准", "major"),
    ("我方无法", "critical"),
    ("无法提供", "critical"),
    ("仅作参考", "minor"),
    ("仅供参考", "minor"),
    ("待补充", "minor"),
)

LABELS = (
    "建设周期",
    "交付周期",
    "工期",
    "质保期",
    "保修期",
    "投标有效期",
    "投标保证金",
    "报价",
    "预算",
)
VALUE_PATTERN = re.compile(
    r"(%s)[^。；\n]{0,40}?(\d+(?:\.\d+)?)\s*(万元|元|天|日|个月|月|年|%%)?" % "|".join(LABELS)
)


def dedupe(sections: list[BidSection]) -> tuple[dict[str, str], list[dict]]:
    """跨章节段落去重：保留首次出现，删除后续重复的长句/段落。"""
    seen: dict[str, str] = {}
    updates: dict[str, str] = {}
    issues: list[dict] = []
    for section in sections:
        body = section.body or ""
        if not body.strip():
            continue
        kept: list[str] = []
        removed = 0
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                kept.append("")
                continue
            key = re.sub(r"\s+", "", stripped)
            if len(key) < 40:
                kept.append(line)
                continue
            if key in seen:
                removed += 1
                continue
            seen[key] = section.heading
            kept.append(line)
        if removed:
            cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
            updates[section.id] = cleaned
            issues.append(
                {
                    "sectionId": section.id,
                    "heading": section.heading,
                    "severity": "minor",
                    "category": "重复",
                    "message": f"删除 {removed} 个与前面章节重复的段落",
                }
            )
    return updates, issues


def consistency(sections: list[BidSection]) -> list[dict]:
    values: dict[str, set[str]] = defaultdict(set)
    where: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for section in sections:
        body = section.body or ""
        for match in VALUE_PATTERN.finditer(body):
            label = match.group(1)
            value = f"{match.group(2)}{match.group(3) or ''}"
            values[label].add(value)
            where[label][value].append(section.heading)
    issues: list[dict] = []
    for label, found in values.items():
        if len(found) <= 1:
            continue
        headings = []
        for value, names in where[label].items():
            headings.extend(names[:2])
        issues.append(
            {
                "sectionId": None,
                "heading": "全文",
                "severity": "major",
                "category": "一致性",
                "message": f"「{label}」出现多个口径：{'、'.join(sorted(found))}（涉及 {'、'.join(dict.fromkeys(headings))}）",
            }
        )
    return issues


def guard(sections: list[BidSection]) -> list[dict]:
    issues: list[dict] = []
    for section in sections:
        body = section.body or ""
        for phrase, severity in BANNED_PHRASES:
            if phrase in body:
                issues.append(
                    {
                        "sectionId": section.id,
                        "heading": section.heading,
                        "severity": severity,
                        "category": "表达",
                        "message": f"出现推诿或占位表述「{phrase}」，需要改写为实质响应",
                    }
                )
    return issues


def completeness(sections: list[BidSection]) -> list[dict]:
    issues: list[dict] = []
    for section in sections:
        chars = len(re.sub(r"\s+", "", section.body or ""))
        target = max(1, section.target_words or 1)
        if chars == 0:
            issues.append(
                {
                    "sectionId": section.id,
                    "heading": section.heading,
                    "severity": "blocker",
                    "category": "完整性",
                    "message": "章节正文为空",
                }
            )
        elif chars < int(target * 0.45):
            issues.append(
                {
                    "sectionId": section.id,
                    "heading": section.heading,
                    "severity": "major",
                    "category": "完整性",
                    "message": f"正文约 {chars} 字，低于目标 {target} 字的 45%",
                }
            )
    return issues


def run_quality(sections: list[BidSection]) -> dict:
    updates, duplicate_issues = dedupe(sections)
    issues = (
        duplicate_issues
        + consistency(sections)
        + guard(sections)
        + completeness(sections)
    )
    return {
        "updates": updates,
        "issues": issues,
        "stats": {
            "sections": len(sections),
            "written": sum(1 for section in sections if (section.body or "").strip()),
            "blockers": sum(1 for issue in issues if issue["severity"] == "blocker"),
            "issues": len(issues),
            "characters": sum(len(re.sub(r"\s+", "", section.body or "")) for section in sections),
        },
    }
