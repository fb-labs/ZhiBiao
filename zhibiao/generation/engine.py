"""正文生成引擎（复刻 3001/zhinbiao2 generation_runner 的写作链路）。

流程：清洗原文 → 事实台账/参数锁/源要求 → 全书写作重点编排 → 逐章生成
（格式文书/材料占位/模型正文）→ 截断续写 → 字数校正 → 风险与质量修订 →
篇幅收敛 → 收尾补全 → 编号/卫生/代理口径归一 → 落库 → 全书一致性后处理。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

from zhibiao.core.bid_integrity import repair_generated_text
from zhibiao.core.bill_of_materials import render_bill_context
from zhibiao.core.chapter_quality import (
    is_tail_truncation_issue,
    quality_repair_prompt,
    validate_chapter_content,
)
from zhibiao.core.chapter_rules import (
    material_insert_placeholder,
    safe_structured_draft,
    shape_chapters,
)
from zhibiao.core.commercial_terms import (
    check_commercial_consistency,
    extract_commercial_terms,
    neutralize_owner_placeholders,
    render_commercial_context,
)
from zhibiao.core.consistency import scan_cross_chapter_conflicts, scan_document_scope
from zhibiao.core.content_plan import PLAN_SYSTEM, fallback_focus, parse_plans, planner_prompt
from zhibiao.core.credit_proofs import credit_proof_body
from zhibiao.core.cross_chapter_dedup import compress_cross_chapter_duplicates
from zhibiao.core.format_forms import format_chapter_text_ex, validate_format_chapter
from zhibiao.core.generation_context import GENERIC_WRITING_RULES, chapter_source_context
from zhibiao.core.image_slots import normalize_image_placeholders
from zhibiao.core.ledger import build_project_ledger, facts_ready, truncate_ledger
from zhibiao.core.legal_forms import legal_form_body
from zhibiao.core.output_guard import (
    canonicalize_placeholders,
    find_forbidden_bid_terms,
    repair_prompt,
)
from zhibiao.core.param_lock import (
    build_param_lock,
    extract_device_registry,
    render_device_parameters,
)
from zhibiao.core.pricing_notes import add_drum_pricing_note
from zhibiao.core.requirements import build_requirement_inventory
from zhibiao.core.source_clean import clean_tender_text
from zhibiao.core.source_fidelity import render_primary_contract, source_fidelity_issues
from zhibiao.core.spec_clauses import render_clause_context
from zhibiao.core.structured_forms import parse_pdf_tables, structure_from_report
from zhibiao.core.subject import classify_subject, industry_guard_text
from zhibiao.core.technical_evidence import (
    extract_material_evidence,
    extract_technical_evidence,
    material_evidence_issues,
    process_scope_issues,
    quantity_breakdown_issues,
    render_grouped_source_clauses,
    render_technical_evidence,
    technical_evidence_issues,
)
from zhibiao.core.tail_repair import (
    apply_tail_fragment,
    incomplete_source_note,
    tail_prompt_for,
    tail_problem,
)
from zhibiao.core.tender_checkup import format_chapter, format_sections
from zhibiao.core.text_hygiene import hygienic_text, suppress_agency_assumptions
from zhibiao.core.text_normalize import normalize_generated_text
from zhibiao.generation.forms import standard_form_body
from zhibiao.generation.heading_fix import renumber_inline
from zhibiao.llm import ChatClient

logger = logging.getLogger(__name__)

WORD_LIMIT_RATIO = 1.5
WORD_LIMIT_SLACK = 600
WORD_TRIM_ROUNDS = 2
TAIL_REPAIR_MAX_TOKENS = 600
TAIL_CONTINUE_ROUNDS = 3
TAIL_CONTINUE_MAX_GROWTH = 1200
MIN_CHAPTER_BODY_CHARS = 20
SHARED_LEDGER_MAX_CHARS = 60000

_END_PUNCT = frozenset("。！？；：.!?;:）)】”\"'…—")
_CONT_PROMPT = (
    "上面是你刚刚写的正文末尾。请紧接着继续写下去：从被中断的那句话继续，"
    "保持相同的语言风格、层级结构与编号体例；不要重复已经写过的任何句子或段落，"
    "不要重新输出标题，不要写任何说明性文字，直接接着写正文。"
)

CHAPTER_HINTS = (
    ("资格", "逐项对照资格条件给出证明材料清单与编号，说明每份证书的发证机关、有效期与年检情况。"),
    ("资质", "列出资质等级、证书编号、有效期，并说明与本次招标要求的对应关系。"),
    ("技术", "写技术路线、系统架构、关键功能实现方式，并对招标参数逐条给出响应值与证明材料来源。"),
    ("方案", "给出完整实施步骤、里程碑时间表（按周/天）、人员分工与交付物清单。"),
    ("实施", "写实施流程、进度计划、里程碑与验收方式，说明关键路径与延误应对。"),
    ("组织", "写明项目组织机构、岗位职责、人员数量与资格，给出人员投入计划。"),
    ("进度", "用表格列出时间节点、关键路径、里程碑与延误应对措施。"),
    ("质量", "写质量控制流程、检查频次、检验标准与不合格处理闭环。"),
    ("安全", "写安全管理制度、现场措施、应急预案与责任分工。"),
    ("售后", "明确响应时限（电话响应时间/到场时间）、服务方式、备件供应与考核办法。"),
    ("培训", "写明培训对象、场次、课时、内容大纲与考核方式。"),
    ("报价", "给出报价构成（人工、设备、软件、税费等）与价格合理性依据。"),
    ("业绩", "列类似项目名称、合同金额、时间、业主与联系人，并说明与本项目的可比性。"),
    ("服务", "写服务内容、服务级别、人员配置与考核指标。"),
    ("风险", "逐项列出项目风险、影响程度与应对措施。"),
    ("验收", "写验收标准、验收流程、验收资料清单与整改闭环。"),
    ("数据", "写数据采集、治理、接口对接方案与数据安全措施。"),
    ("合规", "对照招标文件条款逐条说明合规做法与承诺依据。"),
)

BODY_WRITER_SYSTEM = GENERIC_WRITING_RULES + "\n" + (
    "你是一名资深投标文件撰写专家。只按本次章节范围写正式投标响应正文。\n"
    "事实优先级：招标文件原文与全书统一事实高于一般写作建议。未提供的投标人信息、"
    "证书编号、业绩、专利号、人员姓名、报价、税率、数量和量化承诺不得编造，必须使用"
    "{{待填:具体信息}}占位。\n"
    "保持期限、单位、对象和起算条件的原口径；不同对象的要求分别表述，不得合并或替换。\n"
    "投标正文只写投标人的肯定响应、执行措施和可核查承诺；招标文件中的否决/无效投标风险只供内部核对，"
    "不得把风险条款、风险后果或自我否定结论写入正文。正文不得出现“废标、否决投标、无效投标、投标无效、"
    "否决响应、响应无效、投标文件无效”等表述；相关要求改写为具体、正面的符合性响应。\n"
    "只写本节负责的内容，遵循编排重点与章节路径，不重复相邻章节或下级章节的细节；"
    "分支章节只写范围、总体思路和下级分工。\n"
    "拒绝空话套话和评委解说语。采用清楚的标题、步骤、表格或清单，具体程度以已有依据为限。\n"
    "直接输出正文，不写章节标题前缀、过程说明或结尾客套话。"
)

BODY_WRITER_PROMPT = (
    "请撰写投标文件章节：《{title}》（第 {seq} 节，共 {total} 节）。\n"
    "本节章节号：{number}。本节内部小标题必须冠以 {number}.1、{number}.2 这样的层级编号；"
    "严禁使用全局序号或汉字序号（如「一、」「2」「4.1」「16.1」）。\n"
    "章节完整路径：{path}\n"
    "本节编排重点：{focus}\n"
    "本节具体分工：{scope}\n"
    "本节相关评分项及标准：\n{scores}\n"
    "本节目标篇幅：{length}\n"
    "其他章节标题（只用于划分边界，不得重复其职责）：{others}\n"
    "本次检查指出的问题：\n{issues}\n"
    "正文仅写正面的符合性响应和执行措施，不转述否决/无效投标风险条款及其后果。\n"
    "仅使用上述已确认事实。资料缺失时保留{{{{待填:字段名称}}}}占位符。\n"
    "联系人口径：招标文件里的联系人只用于澄清、答疑、递交等程序性表述；"
    "招标人联系人、电话、现场负责人一律不得留待填——原文给出的直接引用，未给出的写「由招标人指定」；"
    "只有投标人自己的人员、证书、业绩、报价缺失时才写{{{{待填:投标人现场负责人及电话}}}}，"
    "不得用代理机构或平台客服的号码顶替。\n"
    "行业口径：以本次招标实际范围选用机械装备、工程施工、服务、信息化或混合项目的措施。"
    "不得因通用模板新增原文未要求且无设计依据的系统、标准或能力；明确要求的跨行业接口仍须响应。\n"
    "参数纪律：原文数值、单位、材质牌号、公称直径、标准号须与适用对象、工况及层级同时引用。"
    "同对象同工况同层级保持一致；不同口径与原文冲突不得强行合并。\n"
    "直接输出本节正文。"
)


def _safe_format(template: str, fields: dict[str, Any]) -> str:
    class _Missing(dict):
        def __missing__(self, key: Any) -> str:
            return ""

    try:
        return template.format_map(_Missing(fields))
    except Exception:  # noqa: BLE001
        return template


def _chapter_hint(title: str) -> str:
    text = str(title or "")
    for key, hint in CHAPTER_HINTS:
        if key in text:
            return hint
    return "按本节标题给出可核查的实施做法、责任岗位、时间节点与验收标准"


def _scores_context(title: str, focus: str, scores: list[dict[str, Any]]) -> str:
    if not scores:
        return "（暂无与本节匹配的评分项）"

    def grams(text: str) -> set[str]:
        clean = re.sub(r"[\W_]+", "", str(text or ""))
        return {clean[index:index + 2] for index in range(max(0, len(clean) - 1))}

    query = grams(title + " " + focus)
    ranked = []
    for item in scores:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        criteria = str(item.get("criteria") or "")
        score = len(query & grams(name + criteria))
        ranked.append((score, name, criteria, item.get("score")))
    ranked.sort(key=lambda row: row[0], reverse=True)
    chosen = [row for row in ranked[:4] if row[0] > 0] or ranked[:3]
    lines = []
    for _, name, criteria, value in chosen:
        if not name:
            continue
        label = f"{value}分" if value not in (None, "") else "未标注"
        lines.append(f"- {name}（{label}）：{criteria[:300]}")
    return "\n".join(lines) or "（暂无与本节匹配的评分项）"


def _chapter_scope(chapter: dict[str, Any]) -> str:
    title = str(chapter.get("title") or "")
    if re.search(r"偏离|偏差", title):
        return (
            "本节标题含「偏离/偏差」：只作无偏离声明与肯定性响应承诺，不得罗列任何偏离条目、"
            "替代措施或「偏离项登记表」。"
        )
    if chapter.get("has_children"):
        children = "、".join(chapter.get("children") or []) or "下级章节"
        return f"本节是分支总述，只写范围、总体思路和分工，简要说明由{children}分别展开。"
    siblings = "、".join((chapter.get("siblings") or [])[:12]) or "无"
    return f"本节是具体执行章节，围绕标题写可核查做法；同级章节（{siblings}）各自承担内容，不重复。"


def _words(text: str) -> int:
    return len(re.sub(r"\s", "", str(text or "")))


def _looks_incomplete(text: str, target: int) -> bool:
    body = str(text or "").strip()
    if len(body) < 50:
        return False
    ceiling = int(target * WORD_LIMIT_RATIO) if target > 0 else 0
    if ceiling and _words(body) >= ceiling:
        return False
    if target > 0 and _words(body) < int(target * 0.55):
        return True
    tail = body[-1:]
    if tail in _END_PUNCT:
        return any(body.endswith(item) for item in ("如下", "包括", "其中", "例如", "分别"))
    return tail not in _END_PUNCT


def _trim_overlap(prev: str, addition: str) -> str:
    add = str(addition or "").lstrip()
    body = str(prev or "").rstrip()
    if not body or not add:
        return add
    limit = min(len(body), len(add), 400)
    for size in range(limit, 4, -1):
        if body[-size:] == add[:size]:
            add = add[size:].lstrip()
            break
    tail = body.split("\n")[-1].strip()
    if len(tail) >= 20:
        pos = add.find(tail)
        if 0 <= pos <= 200:
            add = add[pos + len(tail):].lstrip()
    return add


def _chapter_llm(client: ChatClient, target_words: int) -> int:
    target = max(0, int(target_words or 0))
    configured = int(getattr(client, "max_tokens", 0) or 10000)
    if target <= 0:
        return configured
    # 中文正文 1 字约 1 token；再留 60% 余量。旧公式按 0.93 倍给预算，
    # 目标 4000 字只给 3700 token，模型必然写不满。
    return max(4500, min(configured, int(target * 1.6) + 600))


def _trim_ceiling(target: int) -> int:
    target = max(0, int(target or 0))
    if target <= 0:
        return 0
    return max(int(target * WORD_LIMIT_RATIO), target + WORD_LIMIT_SLACK)


def _accept_condensed(
    candidate: str, current: str, validate: Callable[[str], list[str]], target: int
) -> tuple[bool, str]:
    text = str(candidate or "").strip()
    cur = _words(current)
    new = _words(text)
    floor = max(200, int(max(0, target) * 0.5)) if target > 0 else int(cur * 0.35)
    if not text:
        return False, "收敛稿为空"
    if new >= cur:
        return False, "收敛稿没有变短"
    if new < floor:
        return False, f"收敛稿过短（{new} 字 < 下限 {floor} 字）"
    issues = validate(text)
    if issues:
        return False, "；".join(issues[:3])
    return True, ""


def _condense_prompt(title: str, text: str, target: int, current: int) -> str:
    return (
        f"以下《{title}》正文约 {current} 字，远超本节目标篇幅约 {target} 字，需要收敛改写。\n"
        f"请压缩到 {target} 字左右（不超过 {int(target * 1.2)} 字）。\n"
        "压缩纪律：删除重复表述、空话套话、泛泛背景与其它章节的复述；不得删除任何具体参数、"
        "数值、单位、牌号、标准号、数量、工期、质保、承诺、评分项响应、表格行与待填占位；"
        "保留小标题层级与编号；不要写成摘要。只输出收敛后的完整正文。\n\n【现有正文】\n"
        + str(text or "")
    )


def _expand_prompt(title: str, text: str, target: int, current: int) -> str:
    return (
        f"以下《{title}》正文只有约 {current} 字，低于本节目标篇幅约 {target} 字。\n"
        "请在不新增无依据事实的前提下补足篇幅：补充已有依据可支撑的实施步骤、分工、控制点、"
        "验收方式、风险应对与保障措施，把空泛表述改写成可核查的具体做法。\n"
        "不得编造专利号、证书编号、业绩、人员姓名、报价或量化承诺；资料缺失处继续使用"
        " {{待填:字段名称}} 占位。只输出补足后的完整正文。\n\n【现有正文】\n"
        + str(text or "")
    )


async def _ask(
    client: ChatClient,
    system: str,
    shared: str,
    prompt: str,
    max_tokens: int | None = None,
) -> str:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": shared},
        {"role": "user", "content": prompt},
    ]
    return await client.chat(messages, max_tokens=max_tokens)


async def _continue_text(
    client: ChatClient,
    system: str,
    shared: str,
    text: str,
    target: int,
    rounds: int = 2,
) -> tuple[str, int]:
    out = str(text or "")
    used = 0
    for _ in range(max(0, int(rounds))):
        if not _looks_incomplete(out, target):
            break
        try:
            addition = await _ask(
                client,
                system,
                shared,
                "【已写正文末尾】\n" + out[-1500:] + "\n\n" + _CONT_PROMPT,
                max_tokens=_chapter_llm(client, target),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[正文生成] 续写异常，保留已写正文：%s", exc)
            break
        piece = _trim_overlap(out, addition)
        if len(piece.strip()) < 20:
            break
        out = out.rstrip() + "\n" + piece.lstrip()
        used += 1
    return out, used


async def _finish_tail(
    client: ChatClient,
    text: str,
    *,
    title: str,
    system: str,
    shared: str,
    source_items: list[str] | None = None,
    truncated: bool = False,
    stop_event: asyncio.Event | None = None,
) -> tuple[str, list[str], bool, str]:
    body = str(text or "")
    notes: list[str] = []
    continued = 0
    for round_index in range(TAIL_CONTINUE_ROUNDS):
        if stop_event is not None and stop_event.is_set():
            break
        problem = tail_problem(
            body,
            source_items=list(source_items or []),
            truncated=bool(truncated and round_index == 0),
        )
        if problem is None:
            break
        prompt = tail_prompt_for(problem, title=title)
        try:
            fragment = await _ask(client, system, shared, prompt, max_tokens=TAIL_REPAIR_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[收尾补全] 章节 %s 调用异常：%s", title, exc)
            notes.append("续写调用异常")
            break
        piece = str(fragment or "").strip()
        if not piece or piece.strip("<> 　").upper() == "OK":
            notes.append("续写不可用")
            break
        candidate = apply_tail_fragment(body, problem, piece)
        growth = _words(candidate) - _words(body)
        if candidate == body or growth <= 0:
            notes.append("续写无有效增量")
            break
        if growth > TAIL_CONTINUE_MAX_GROWTH:
            notes.append("续写内容过长")
            break
        body = candidate
        continued += 1
        notes.append("续写表格缺行" if str(problem.get("mode")) == "append" else "补全断点单元")
    remaining = tail_problem(
        body,
        source_items=list(source_items or []),
        truncated=bool(truncated and not continued),
    )
    if remaining is not None:
        return body, notes + ["未完成，已标注人工补齐"], True, incomplete_source_note(remaining)
    return body, notes, False, ""


def _blocking(issues: list[str]) -> list[str]:
    return [item for item in list(issues or []) if not is_tail_truncation_issue(item)]


def _validate(
    text: str,
    title: str,
    *,
    device_registry: list[dict[str, Any]],
    tender_text: str,
    chapter_number: str,
    allow_material: bool,
    commercial: dict[str, str],
) -> list[str]:
    issues = validate_chapter_content(
        text,
        title,
        device_registry,
        tender_text,
        allow_material=allow_material,
        chapter_number=chapter_number,
        strict_completeness=True,
    )
    if not allow_material:
        issues += source_fidelity_issues(text, title, tender_text)
        issues += check_commercial_consistency(commercial, text)
    return list(dict.fromkeys(issues))


def _source_table_items(tender_text: str, hint: str) -> list[str]:
    try:
        from zhibiao.core.table_completeness import tender_requirement_items

        return list(tender_requirement_items(str(tender_text or ""), hint=str(hint or "")) or [])
    except Exception:  # noqa: BLE001
        return []


def _prepare_dims(analysis: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dims: list[dict[str, Any]] = []
    if analysis:
        for item in getattr(analysis, "dimensions", None) or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            if label and value and value != "未明确":
                dims.append({"name": label, "value": value, "hit": True})
    scores: list[dict[str, Any]] = []
    if analysis:
        raw = (getattr(analysis, "scoring", None) or {}).get("items") or []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            scores.append(
                {
                    "name": name,
                    "criteria": str(item.get("criteria") or ""),
                    "score": item.get("points", item.get("score")),
                }
            )
    return dims, scores


def build_context(
    tender: Any, analysis: Any, tender_text: str, pdf_bytes: bytes | None = None
) -> dict[str, Any]:
    raw = str(tender_text or "")
    clean = clean_tender_text(raw) if raw else ""
    pdf_report: dict[str, Any] = {}
    if pdf_bytes:
        try:
            pdf_report = parse_pdf_tables(bytes(pdf_bytes))
        except Exception as exc:  # noqa: BLE001 - 原件解析失败不能拖垮生成
            logger.warning("[正文生成] 招标原件表格解析失败：%s", exc)
    technical_evidence = extract_technical_evidence(pdf_report) if pdf_report else []
    material_evidence = extract_material_evidence(pdf_report) if pdf_report else []
    technical_context = render_technical_evidence(technical_evidence) if technical_evidence else ""
    grouped_clauses = render_grouped_source_clauses(pdf_report) if pdf_report else ""
    format_span = format_chapter(clean) if clean else None
    format_anchor = str((format_span or {}).get("title") or "").strip() or None
    format_items = format_sections(clean) if clean else []
    format_titles = [str(item.get("title") or "") for item in format_items]
    format_labels = [str(item.get("label") or "") for item in format_items]
    dims, scores = _prepare_dims(analysis)
    project_name = str(getattr(tender, "title", "") or "")
    if not any(item["name"] in ("项目名称", "招标项目名称") for item in dims):
        dims.insert(0, {"name": "项目名称", "value": project_name, "hit": True})
    if not any(item["name"] in ("项目编号", "招标编号") for item in dims):
        dims.insert(1, {"name": "项目编号", "value": str(getattr(tender, "code", "") or ""), "hit": True})
    param_lock = build_param_lock(clean) if clean else ""
    subject = classify_subject(clean) if clean else {}
    industry_text = industry_guard_text(subject) if isinstance(subject, dict) else ""
    commercial = extract_commercial_terms(clean) if clean else {}
    commercial_context = render_commercial_context(commercial)
    bill_context = render_bill_context(clean) if clean else ""
    requirement_inventory = build_requirement_inventory(clean) if clean else {"requirements": []}
    source_contract = render_primary_contract(clean) if clean else ""
    facts, ledger_text = build_project_ledger(
        project_name,
        dims,
        scores,
        param_lock=param_lock,
        industry_text=industry_text,
        tender_text=clean,
    )
    device_registry = extract_device_registry(clean) if clean else []
    return {
        "clean": clean,
        "dims": dims,
        "scores": scores,
        "facts": facts,
        "ledger": ledger_text,
        "param_lock": param_lock,
        "commercial": commercial,
        "commercial_context": commercial_context,
        "bill_context": bill_context,
        "requirements": requirement_inventory,
        "source_contract": source_contract,
        "device_registry": device_registry,
        "format_span": format_span,
        "format_sections": format_items,
        "format_titles": format_titles,
        "format_labels": format_labels,
        "format_anchor": format_anchor,
        "pdf_report": pdf_report,
        "technical_evidence": technical_evidence,
        "material_evidence": material_evidence,
        "technical_context": technical_context,
        "grouped_clauses": grouped_clauses,
    }


def _shared_ledger(ledger_text: str) -> str:
    value = str(ledger_text or "")
    if len(value) > SHARED_LEDGER_MAX_CHARS:
        value = truncate_ledger(value, SHARED_LEDGER_MAX_CHARS) + (
            "\n[台账超长，已按小节配额压缩展示；完整台账见项目解读结果]"
        )
    return "全书统一事实与承诺口径（逐字遵守，不把不同对象混为一谈）：\n" + value


async def plan_focus(
    client: ChatClient | None,
    chapters: list[dict[str, Any]],
    ledger: str,
    *,
    stop_event: asyncio.Event | None = None,
) -> list[dict[str, Any]]:
    shaped = shape_chapters(chapters)
    siblings: dict[str, list[str]] = {}
    for chapter in shaped:
        siblings.setdefault(str(chapter.get("parent_id") or ""), []).append(chapter["title"])
    for chapter in shaped:
        peers = siblings.get(str(chapter.get("parent_id") or ""), [])
        chapter["siblings"] = [title for title in peers if title != chapter["title"]]
    missing = [item for item in shaped if not str(item.get("writing_focus") or "").strip()]
    if missing and client is not None and not (stop_event and stop_event.is_set()):
        prompt = planner_prompt(shaped, ledger)
        try:
            raw = await _ask(client, PLAN_SYSTEM, ledger, prompt, max_tokens=4096)
            plans = parse_plans(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[正文编排] 编排失败，使用确定性分工：%s", exc)
            plans = {}
        for chapter in missing:
            chapter["writing_focus"] = plans.get(chapter["seq"]) or fallback_focus(
                chapter, _chapter_hint(str(chapter.get("title") or ""))
            )
    for chapter in shaped:
        if not str(chapter.get("writing_focus") or "").strip():
            chapter["writing_focus"] = fallback_focus(
                chapter, _chapter_hint(str(chapter.get("title") or ""))
            )
    return shaped


async def generate_chapter(
    client: ChatClient | None,
    chapter: dict[str, Any],
    *,
    context: dict[str, Any],
    chapters: list[dict[str, Any]],
    numbers: dict[str, str],
    containers: set[str],
    budgets: dict[str, int],
    issues: list[str],
    stop_event: asyncio.Event | None = None,
    chapters_before: list[dict[str, Any]] | None = None,
) -> tuple[str, str, str]:
    """返回 (正文, 来源说明, 错误)。错误为空表示成功。"""
    title = str(chapter.get("title") or "")
    if not title.strip():
        return "", "", "章节标题为空"
    number = str(numbers.get(str(chapter.get("id")) or "") or chapter.get("seq") or "")
    focus = str(chapter.get("writing_focus") or "")
    clean = context["clean"]
    ledger = context["ledger"]
    dims = context["dims"]
    scores = context["scores"]
    commercial = context["commercial"]
    device_registry = context["device_registry"]

    source_context = chapter_source_context(
        context["requirements"],
        [],
        str(chapter.get("path") or title),
        focus,
    )
    shared = _shared_ledger(ledger)
    if context["commercial_context"]:
        shared += "\n\n" + context["commercial_context"]
    if context["source_contract"]:
        shared += "\n\n" + context["source_contract"]
    if context["bill_context"]:
        shared += "\n\n" + context["bill_context"]
    clause = render_clause_context(clean, str(chapter.get("path") or title), focus)
    if clause:
        shared += "\n\n" + clause
    device_table = render_device_parameters(device_registry, prefer=title + " " + focus)
    if device_table:
        shared += "\n\n【设备参数表（招标原文，逐项照抄，不得混用）】\n" + device_table
    hint = title + " " + focus
    if context.get("technical_context") and re.search(r"技术|参数|性能|设备|供货|配置|结构", hint):
        shared += "\n\n" + context["technical_context"]
    if context.get("grouped_clauses") and re.search(r"技术|参数|性能|材质|材料|设备|结构|工艺|涂装", hint):
        shared += "\n\n" + context["grouped_clauses"]
    if context.get("material_evidence") and re.search(r"技术|参数|性能|材质|材料|设备|结构", hint):
        shared += "\n\n【材质表逐行依据】\n" + "\n".join(
            "PDF第%s页｜%s｜%s" % (item["page"], item["owner"], item["value"])
            for item in context["material_evidence"][:100]
        )
    if source_context:
        shared += "\n\n" + source_context

    legal_text = legal_form_body(title, tender_text=clean, dims=dims)
    credit_text = credit_proof_body(title, clean)
    form_structure = structure_from_report(
        context.get("pdf_report"),
        title,
        titles=context.get("format_titles") or (),
        labels=context.get("format_labels") or (),
        chapter_anchor=context.get("format_anchor"),
    )
    format_text, form_meta = (None, {}) if (legal_text or credit_text) else format_chapter_text_ex(
        title, clean, context["facts"], structure=form_structure
    )
    standard_text = None if (legal_text or credit_text or format_text) else standard_form_body(
        title, dims, clean
    )
    material_text = (
        legal_text
        or credit_text
        or format_text
        or standard_text
        or (None if chapter.get("has_children") else material_insert_placeholder(title))
    )
    form_text = bool(legal_text or format_text or standard_text)

    source = ""
    text = ""
    truncated = False
    if legal_text:
        text, source = legal_text, "格式文书（招标格式原文/通用格式，红色待填）"
    elif credit_text:
        text, source = credit_text, "规则生成（信誉查询渠道逐条 + 每渠道一个截图图框）"
    elif format_text:
        text, source = format_text, "招标格式原文（表格/条款按原文照录）"
    elif standard_text:
        text, source = standard_text, "内置标准格式（按招标文件要求填写）"
    elif material_text:
        text, source = material_text, "规则占位（材料原件需插入）"
    elif client is None:
        text, source = safe_structured_draft(title, context["facts"], scores), "内置模板（结构草稿，需人工补充）"
    else:
        target = int(budgets.get(str(chapter.get("id")) or "", 0) or 0)
        length_hint = (
            "篇幅不限，按本节实际任务展开，不添加无依据内容"
            if target <= 0
            else f"约 {target} 字，硬性上限 {int(target * WORD_LIMIT_RATIO)} 字"
        )
        prompt = _safe_format(
            BODY_WRITER_PROMPT,
            {
                "title": title,
                "seq": chapter.get("seq"),
                "number": number,
                "total": len(chapters),
                "path": chapter.get("path") or title,
                "focus": focus or "按本节标题和全书事实确定内容范围",
                "scope": _chapter_scope(chapter),
                "scores": _scores_context(title, focus, scores)[:5000],
                "length": length_hint,
                "others": "、".join(
                    f"{numbers.get(str(item.get('id')) or '', '')} {item.get('title') or ''}"
                    for item in chapters
                    if str(item.get("id")) != str(chapter.get("id"))
                )[:4000] or "无",
                "issues": "\n".join(f"- {item}" for item in list(issues or [])[:10]) or "（无）",
            },
        )
        try:
            text = await _ask(client, BODY_WRITER_SYSTEM, shared, prompt, max_tokens=_chapter_llm(client, target))
            source = f"大模型 {getattr(client, 'model', '')}".strip()
            text, rounds = await _continue_text(client, BODY_WRITER_SYSTEM, shared, text, target)
            if rounds:
                source += f"（截断续写×{rounds}）"
            truncated = False
        except Exception as exc:  # noqa: BLE001
            logger.warning("[正文生成] 章节 %s 模型调用失败：%s", title, exc)
            return "", "", f"模型调用失败：{exc}"

    if not text.strip():
        return "", "", "模型未返回正文"

    target = int(budgets.get(str(chapter.get("id")) or "", 0) or 0)
    if client is not None and target > 0 and not material_text and not form_text:
        words = _words(text)
        if words < int(target * 0.75) and not (stop_event and stop_event.is_set()):
            try:
                expanded = await _ask(
                    client,
                    BODY_WRITER_SYSTEM,
                    shared,
                    _expand_prompt(title, text, target, words),
                    max_tokens=_chapter_llm(client, target),
                )
                if _words(expanded) > words:
                    text = expanded.strip()
                    source += "（字数校正）"
            except Exception as exc:  # noqa: BLE001
                logger.warning("[字数校正] 章节 %s 扩写失败：%s", title, exc)

    if not form_text and not material_text:
        forbidden = find_forbidden_bid_terms(text)
        if forbidden and client is not None:
            try:
                revised = await _ask(
                    client,
                    BODY_WRITER_SYSTEM,
                    shared,
                    repair_prompt(title, text, forbidden),
                    max_tokens=_chapter_llm(client, target or 1200),
                )
                if revised.strip():
                    text = revised.strip()
                    source += "（正文风险修订）"
            except Exception as exc:  # noqa: BLE001
                logger.warning("[正文风险] 章节 %s 修订失败：%s", title, exc)
            forbidden = find_forbidden_bid_terms(text)
        if forbidden:
            return "", "", "正文包含否决/无效投标风险表述，修订后仍未通过"

    if not form_text:
        text, renumbered = renumber_inline(text, number, containers)
        if renumbered:
            source = (source + "（内嵌编号归一）").strip()

    if form_text:
        quality_issues = validate_format_chapter(text, title)
    else:
        quality_issues = _validate(
            text,
            title,
            device_registry=device_registry,
            tender_text=clean,
            chapter_number=number,
            allow_material=bool(material_text),
            commercial=commercial,
        )

    blocking = _blocking(quality_issues)
    if blocking and client is not None and not material_text and not (stop_event and stop_event.is_set()):
        for _ in range(2):
            try:
                fixed = await _ask(
                    client,
                    BODY_WRITER_SYSTEM,
                    shared,
                    quality_repair_prompt(title, text, quality_issues),
                    max_tokens=_chapter_llm(client, target or 1200),
                )
                if fixed.strip() and _words(fixed) >= min(200, _words(text)):
                    text = fixed.strip()
                    if not form_text:
                        text, _ = renumber_inline(text, number, containers)
                    source += "（来源质量修订）"
            except Exception as exc:  # noqa: BLE001
                logger.warning("[正文质量] 章节 %s 修订失败：%s", title, exc)
            quality_issues = _validate(
                text,
                title,
                device_registry=device_registry,
                tender_text=clean,
                chapter_number=number,
                allow_material=bool(material_text),
                commercial=commercial,
            ) + ["修订后出现禁止风险表述：" + x for x in find_forbidden_bid_terms(text)]
            blocking = _blocking(quality_issues)
            if not blocking:
                break
    if blocking:
        return "", "", "正文质量未通过：" + "；".join(blocking[:5])

    if client is not None and target > 0 and not material_text and not form_text:
        ceiling = _trim_ceiling(target)
        for _ in range(WORD_TRIM_ROUNDS):
            current = _words(text)
            if ceiling <= 0 or current <= ceiling:
                break
            try:
                condensed = await _ask(
                    client,
                    BODY_WRITER_SYSTEM,
                    shared,
                    _condense_prompt(title, text, target, current),
                    max_tokens=_chapter_llm(client, target),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[篇幅收敛] 章节 %s 收敛失败：%s", title, exc)
                break
            accepted, why = _accept_condensed(
                condensed,
                text,
                lambda body: _validate(
                    body,
                    title,
                    device_registry=device_registry,
                    tender_text=clean,
                    chapter_number=number,
                    allow_material=False,
                    commercial=commercial,
                )
                + ["收敛后出现禁止风险表述：" + x for x in find_forbidden_bid_terms(body)],
                target,
            )
            if not accepted:
                logger.warning("[篇幅收敛] 章节 %s 收敛稿被拒（%s），保留原稿", title, why)
                break
            text = condensed.strip()
            if not form_text:
                text, _ = renumber_inline(text, number, containers)
            source += "（篇幅收敛）"

    if client is not None and text and not form_text and not material_text:
        last = (text.rstrip().splitlines() or [""])[-1].strip()
        source_items = _source_table_items(clean, title) if last.startswith("|") else []
        text, notes, incomplete, note = await _finish_tail(
            client,
            text,
            title=title,
            system=BODY_WRITER_SYSTEM,
            shared=shared,
            source_items=source_items,
            truncated=truncated,
            stop_event=stop_event,
        )
        if notes:
            source += "（" + "、".join(dict.fromkeys(notes)) + "）"
        if incomplete:
            source += "（收尾未完成：incomplete，需人工补齐）"
            if note:
                source += f"（{note}）"

    if not form_text and not material_text:
        try:
            from zhibiao.core.table_join import (
                complete_truncated_paragraphs,
                join_split_table_rows,
            )

            text, _ = join_split_table_rows(text)
            text, _ = complete_truncated_paragraphs(text)
        except Exception:  # noqa: BLE001
            pass
        text, _ = repair_generated_text(text, title, project_name=str(getattr(chapter, "project_name", "") or ""))

    text, _ = neutralize_owner_placeholders(text)
    text, _ = normalize_image_placeholders(text)
    if not form_text:
        numbers_all = set(numbers.values())
        text, _ = normalize_generated_text(text, numbers_all)
    text, _ = hygienic_text(text)
    if not form_text:
        text, _ = suppress_agency_assumptions(text, clean)
    text, _ = add_drum_pricing_note(text)
    if not form_text and not material_text:
        final_technical = (
            technical_evidence_issues(text, context.get("technical_evidence") or [])
            + quantity_breakdown_issues(text)
            + material_evidence_issues(text, context.get("material_evidence") or [])
            + process_scope_issues(text, clean)
        )
        if final_technical:
            return "", "", "原表参数核对未通过：" + "；".join(final_technical[:5])
    if _words(text) < MIN_CHAPTER_BODY_CHARS and not form_text and not material_text:
        return "", "", f"正文为空或几乎为空（{_words(text)} 字）"
    return text.strip(), source, ""


def finalize_document(
    rows: list[dict[str, Any]], tender_text: str
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """全书后处理：占位符归一、跨章冲突告警、跨章重复压缩。"""
    updates: dict[str, str] = {}
    issues: list[dict[str, Any]] = []
    for row in rows:
        body = str(row.get("content") or "")
        if not body.strip():
            continue
        fixed, count = canonicalize_placeholders(body)
        fixed, neutralized = neutralize_owner_placeholders(fixed)
        if count or neutralized:
            updates[str(row.get("id"))] = fixed
    try:
        conflicts = scan_cross_chapter_conflicts(rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[正文一致性] 跨章核对失败：%s", exc)
        conflicts = []
    try:
        scoped = scan_document_scope(rows, tender_text)
        conflicts = list(conflicts or []) + list(scoped or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[正文一致性] 供货边界核对失败：%s", exc)
    for item in conflicts or []:
        issues.append(
            {
                "sectionId": str(item.get("id") or item.get("section_id") or "") or None,
                "heading": str(item.get("title") or ""),
                "severity": "major",
                "category": "一致性",
                "message": str(item.get("detail") or item.get("message") or item),
            }
        )
    try:
        deduped, dedup_notes = compress_cross_chapter_duplicates(
            [
                {
                    "id": r.get("id"),
                    "seq": r.get("seq"),
                    "level": r.get("level"),
                    "title": r.get("title"),
                    "content": str(r.get("content") or ""),
                }
                for r in rows
            ],
            tender_text,
            enabled=True,
        )
        for chapter_id, text in (deduped or {}).items():
            updates[str(chapter_id)] = text
            issues.append(
                {
                    "sectionId": str(chapter_id),
                    "heading": "",
                    "severity": "minor",
                    "category": "重复",
                    "message": "删除跨章重复段落",
                }
            )
        for note in dedup_notes or []:
            logger.info("[跨章去重] %s", note)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[跨章去重] 失败：%s", exc)
    return updates, issues


def check_facts(tender: Any, analysis: Any) -> tuple[bool, str]:
    dims, _ = _prepare_dims(analysis)
    return facts_ready(str(getattr(tender, "title", "") or ""), dims)
