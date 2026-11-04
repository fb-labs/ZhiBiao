from __future__ import annotations

import re
from typing import Any

from zhibiao.models import Tender

FACT_KEY = "agent_facts"


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def normalize_facts(raw: dict | None) -> dict:
    data = raw if isinstance(raw, dict) else {}
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    bidder = data.get("bidder") if isinstance(data.get("bidder"), dict) else {}
    pricing = data.get("pricing") if isinstance(data.get("pricing"), dict) else {}
    template = data.get("template") if isinstance(data.get("template"), dict) else {}
    return {
        "project": {
            "name": _text(project.get("name")),
            "code": _text(project.get("code"), 80),
            "buyer": _text(project.get("buyer")),
            "budget": _text(project.get("budget"), 80),
            "deadline": _text(project.get("deadline"), 80),
            "bidValidity": _text(project.get("bidValidity"), 80),
            "duration": _text(project.get("duration"), 80),
            "deposit": _text(project.get("deposit"), 80),
        },
        "bidder": {
            "name": _text(bidder.get("name")),
            "aliases": [_text(item) for item in _list(bidder.get("aliases")) if _text(item)],
        },
        "materials": [
            {
                "name": _text(item.get("name") if isinstance(item, dict) else item),
                "note": _text(item.get("note") if isinstance(item, dict) else "", 300),
            }
            for item in _list(data.get("materials"))
            if _text(item.get("name") if isinstance(item, dict) else item)
        ],
        "parameters": [
            {
                "name": _text(item.get("name") if isinstance(item, dict) else item),
                "required": _text(item.get("required") if isinstance(item, dict) else "", 160),
                "unit": _text(item.get("unit") if isinstance(item, dict) else "", 40),
            }
            for item in _list(data.get("parameters"))
            if _text(item.get("name") if isinstance(item, dict) else item)
        ],
        "qualifications": [_text(item, 300) for item in _list(data.get("qualifications")) if _text(item)],
        "commitments": [_text(item, 300) for item in _list(data.get("commitments")) if _text(item)],
        "pricing": {
            "maxPrice": _text(pricing.get("maxPrice"), 80),
            "currency": _text(pricing.get("currency"), 20),
        },
        "signature": data.get("signature") if isinstance(data.get("signature"), dict) else {},
        "copies": data.get("copies") if isinstance(data.get("copies"), dict) else {},
        "template": {
            "modeHint": _text(template.get("modeHint"), 40),
            "chapters": [_text(item, 200) for item in _list(template.get("chapters")) if _text(item)],
        },
        "manualChecklist": [
            _text(item, 300) for item in _list(data.get("manualChecklist")) if _text(item)
        ],
    }


def load_facts(tender: Tender) -> dict | None:
    meta = tender.meta or {}
    value = meta.get(FACT_KEY)
    return normalize_facts(value) if isinstance(value, dict) else None


def assign_facts(tender: Tender, raw: dict) -> dict:
    facts = normalize_facts(raw)
    meta = dict(tender.meta or {})
    meta[FACT_KEY] = facts
    tender.meta = meta
    return facts


def fact_keywords(facts: dict, limit: int = 20) -> list[str]:
    keywords: list[str] = []
    for item in facts.get("materials") or []:
        keywords.append(str(item.get("name") or ""))
    for item in facts.get("parameters") or []:
        keywords.append(str(item.get("name") or ""))
    keywords.extend(facts.get("qualifications") or [])
    keywords.extend(facts.get("commitments") or [])
    return [re.sub(r"\s+", "", item)[:40] for item in keywords if item][:limit]
