from __future__ import annotations

import re

from sqlalchemy import select

from zhibiao.agents.audit import run_audit
from zhibiao.agents.base import AgentContext, ToolSpec
from zhibiao.agents.facts import assign_facts, load_facts, normalize_facts
from zhibiao.corpus import tender_corpus
from zhibiao.database import session_factory
from zhibiao.generation.outline import build_outline as build_plan
from zhibiao.models import (
    Analysis,
    BidDocument,
    BidSection,
    Job,
    Review,
    ReviewIssue,
    Tender,
    TenderFile,
)


async def _tender(db, ctx: AgentContext) -> Tender | None:
    return await db.get(Tender, ctx.tender_id)


async def _document(db, tender_id: str) -> BidDocument | None:
    return (
        await db.execute(
            select(BidDocument)
            .where(BidDocument.tender_id == tender_id)
            .order_by(BidDocument.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _sections(db, document: BidDocument | None) -> list[BidSection]:
    if not document:
        return []
    return list(
        (
            await db.execute(
                select(BidSection)
                .where(BidSection.document_id == document.id)
                .order_by(BidSection.position.asc())
            )
        ).scalars().all()
    )


async def _analysis(db, tender_id: str) -> Analysis | None:
    return (
        await db.execute(
            select(Analysis)
            .where(Analysis.tender_id == tender_id, Analysis.is_current.is_(True))
            .order_by(Analysis.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def read_tender(ctx: AgentContext, args: dict) -> dict:
    limit = int(args.get("limit") or 8000)
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        corpus = await tender_corpus(db, tender, limit=max(1000, min(limit, 20000)))
    return {"text": corpus}


async def tender_digest(ctx: AgentContext, _args: dict) -> dict:
    """返回压缩后的招标要点，避免整份正文反复进入模型上下文。"""
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        corpus = await tender_corpus(db, tender, limit=90000)
    lines = [line.strip() for line in corpus.splitlines() if line.strip()]
    keywords = (
        "项目名称",
        "项目编号",
        "采购人",
        "预算",
        "最高限价",
        "投标截止",
        "开标时间",
        "投标有效期",
        "工期",
        "建设周期",
        "交付",
        "投标保证金",
        "资格",
        "资质",
        "营业执照",
        "业绩",
        "投标文件",
        "组成",
        "格式",
        "技术参数",
        "参数",
        "并发",
        "数量",
        "标准",
        "承诺",
        "等保",
        "售后",
        "质保",
        "评分",
        "正本",
        "副本",
        "封装",
        "签字",
        "盖章",
    )
    picked: list[str] = []
    seen: set[str] = set()
    for line in lines[:2000]:
        if len(line) > 300:
            continue
        if any(keyword in line for keyword in keywords) or re.match(
            r"^第[一二三四五六七八九十\d]+[章节]|[一二三四五六七八九十]+[、.．]|\d+(\.\d+)+",
            line,
        ):
            clean = line[:200]
            if clean not in seen:
                seen.add(clean)
                picked.append(clean)
        if len(picked) >= 45:
            break
    digest = "\n".join(picked)[:5500]
    return {"digest": digest, "lines": len(picked)}


async def search_tender(ctx: AgentContext, args: dict) -> dict:
    keyword = str(args.get("keyword") or "").strip()
    if not keyword:
        return {"error": "缺少 keyword"}
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        corpus = await tender_corpus(db, tender, limit=90000)
    hits = []
    for number, line in enumerate(corpus.splitlines(), start=1):
        if keyword in line:
            hits.append({"line": number, "text": line.strip()[:300]})
            if len(hits) >= 20:
                break
    return {"keyword": keyword, "hits": hits}


async def read_analysis(ctx: AgentContext, _args: dict) -> dict:
    async with session_factory()() as db:
        analysis = await _analysis(db, ctx.tender_id)
    if not analysis:
        return {"error": "尚未执行招标解读"}
    return {
        "overview": analysis.overview,
        "dimensions": analysis.dimensions,
        "scoring": analysis.scoring,
        "requirements": analysis.requirements,
        "risks": analysis.risks,
    }


async def read_facts(ctx: AgentContext, _args: dict) -> dict:
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        facts = load_facts(tender)
    return {"facts": facts} if facts else {"facts": None}


async def save_facts(ctx: AgentContext, args: dict) -> dict:
    raw = args.get("facts") if isinstance(args.get("facts"), dict) else args
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        facts = assign_facts(tender, normalize_facts(raw))
        await db.commit()
    return {"saved": True, "facts": facts, "__final__": True}


async def detect_mode(ctx: AgentContext, _args: dict) -> dict:
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        rows = (
            await db.execute(select(TenderFile).where(TenderFile.tender_id == tender.id))
        ).scalars().all()
        facts = load_facts(tender)
        corpus = await tender_corpus(db, tender, limit=160000)
    text_plans, text_note = build_plan("tender", corpus)
    template_docx = [row for row in rows if row.role == "template" and row.filename.lower().endswith(".docx")]
    template_pdf = [row for row in rows if row.role == "template" and row.filename.lower().endswith(".pdf")]
    if len(text_plans) >= 3:
        mode = "text_template"
        reason = f"{text_note}（{sum(1 for p in text_plans if p.level == 1)} 个一级章）"
    elif template_docx or any(row.filename.lower().endswith(".docx") for row in rows):
        mode = "docx_template"
        reason = "检测到 DOCX 格式模板/招标文件，按 Word 模板结构生成"
    elif template_pdf or any(row.filename.lower().endswith(".pdf") for row in rows):
        mode = "pdf_template"
        reason = "检测到 PDF 模板/招标文件，按 PDF 模板章节生成"
    else:
        mode = "generic"
        reason = "未检测到模板文件，使用通用投标结构"
    hint = ((facts or {}).get("template") or {}).get("modeHint")
    if hint in {"generic", "docx_template", "pdf_template"}:
        mode, reason = hint, f"事实抽取建议模式：{hint}"
    return {"mode": mode, "reason": reason}


async def extract_template(ctx: AgentContext, _args: dict) -> dict:
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        rows = (
            await db.execute(
                select(TenderFile)
                .where(TenderFile.tender_id == tender.id)
                .order_by(TenderFile.created_at.desc())
            )
        ).scalars().all()
        facts = load_facts(tender)
        corpus = await tender_corpus(db, tender, limit=60000)
    chapters: list[str] = []
    source = ""
    docx = next((row for row in rows if row.filename.lower().endswith(".docx")), None)
    if docx:
        source = f"DOCX：{docx.filename}"
        try:
            from docx import Document

            document = Document(docx.stored_path)
            for paragraph in document.paragraphs:
                text = paragraph.text.strip()
                style = (paragraph.style.name or "").lower() if paragraph.style else ""
                if not text or len(text) > 60:
                    continue
                if style.startswith("heading") or re.match(r"^(第[一二三四五六七八九十\d]+[章节]|[一二三四五六七八九十]+[、.．]|\d+(\.\d+)*[、.．\s])", text):
                    chapters.append(text)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"DOCX 模板解析失败：{exc}"}
    elif ((facts or {}).get("template") or {}).get("chapters"):
        chapters = list(((facts or {}).get("template") or {}).get("chapters") or [])
        source = "招标解析 Agent 抽取的模板章节"
    else:
        plans = build_plan("tender", corpus)[0]
        chapters = [plan.heading for plan in plans]
        source = "招标文件格式章（文本解析）"
    seen: list[str] = []
    for item in chapters:
        clean = re.sub(r"\s+", "", item)
        if clean and clean not in {re.sub(r"\s+", "", existing) for existing in seen}:
            seen.append(item.strip(" 　\t、，。：:")[:120])
    return {"source": source, "chapters": seen[:40]}


async def save_outline(ctx: AgentContext, args: dict) -> dict:
    """生成并保存大纲；内部走 API 同一份 build_outline_inline。"""
    from zhibiao.api.outline import OutlineOptions, build_outline_inline

    mode = str(args.get("mode") or "generic")
    mapped = (
        "tender"
        if mode in {"docx_template", "pdf_template", "text_template", "tender"}
        else "standard"
    )
    options = OutlineOptions(
        mode=mapped,
        targetWords=int(args.get("targetWords") or 80000),
        templateId=args.get("templateId") or None,
    )
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        data = await build_outline_inline(db, tender, options, ctx.account)
        await db.commit()
    return {
        "mode": data.get("mode"),
        "note": data.get("note"),
        "sections": len(data.get("sections") or []),
        "targetWords": data.get("targetWords"),
    }


async def build_analysis(ctx: AgentContext, _args: dict) -> dict:
    """生成招标解读报告；内部走 API 同一份 analyze_inline。"""
    from zhibiao.api.analysis import analyze_inline

    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        try:
            analysis = await analyze_inline(db, tender, ctx.account)
        except Exception as exc:  # noqa: BLE001 - HTTPException 也在这里转成工具错误
            return {"error": str(getattr(exc, "detail", exc))}
    return {
        "analysisId": analysis.id,
        "dimensions": len(analysis.dimensions or []),
        "scoringItems": len((analysis.scoring or {}).get("items") or []),
        "requirements": len(analysis.requirements or []),
        "risks": len(analysis.risks or []),
    }


async def save_review(ctx: AgentContext, args: dict) -> dict:
    """按当前检查模式落一次质量检查；内部走 API 同一份 run_review_inline。"""
    from zhibiao.api.reviews import run_review_inline

    mode = str(args.get("mode") or "mixed")
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        try:
            review = await run_review_inline(db, tender, mode, ctx.account)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(getattr(exc, "detail", exc))}
    payload = review.payload or {}
    return {
        "reviewId": review.id,
        "mode": review.mode,
        "issues": payload.get("issueCount", 0),
        "score": review.score,
        "verdict": review.verdict,
        "summary": review.summary,
    }


async def save_fix(ctx: AgentContext, args: dict) -> dict:
    """为指定问题生成可应用的修订建议；内部走 API 同一份 propose_fix_inline。"""
    from zhibiao.api.reviews import propose_fix_inline

    review_id = str(args.get("reviewId") or "")
    issue_id = str(args.get("issueId") or "")
    async with session_factory()() as db:
        review = await db.get(Review, review_id) if review_id else None
        issue = await db.get(ReviewIssue, issue_id) if issue_id else None
        if not review or not issue or issue.review_id != review.id:
            return {"error": "检查问题不存在"}
        try:
            proposal = await propose_fix_inline(db, review, issue)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(getattr(exc, "detail", exc))}
        await db.commit()
    return {
        "proposalId": proposal.id,
        "sectionId": proposal.section_id,
        "chars": len(proposal.proposed_text or ""),
    }


async def write_book(ctx: AgentContext, args: dict) -> dict:
    """执行全书写作；内部走 generation.book 的同一份实现，保留逐章进度与重试。"""
    from zhibiao.generation.book import run_book
    from zhibiao.generation.runner import generate_body, manager

    async with session_factory()() as db:
        job = await db.get(Job, ctx.job_id) if ctx.job_id else None
        payload = dict((job.payload if job else {}) or {})
    mode = str(args.get("mode") or payload.get("mode") or "missing")
    cancel = manager.cancel_event(ctx.job_id)
    result = await run_book(
        tender_id=ctx.tender_id,
        section_ids=payload.get("sectionIds"),
        mode=mode,
        cancel=cancel,
        progress=ctx.progress,
        body_writer=generate_body,
    )
    return {**result, "__final__": True}


async def write_section(ctx: AgentContext, args: dict) -> dict:
    """重写单章；与批量生成共用同一份 run_book，命令口径、去重、质检都一致。"""
    from zhibiao.generation.book import run_book
    from zhibiao.generation.runner import generate_body, manager

    section_id = str(args.get("sectionId") or "")
    if not section_id:
        return {"error": "缺少 sectionId"}
    cancel = manager.cancel_event(ctx.job_id)
    result = await run_book(
        tender_id=ctx.tender_id,
        section_ids=[section_id],
        mode="all",
        cancel=cancel,
        progress=ctx.progress,
        body_writer=generate_body,
    )
    return {**result, "sectionId": section_id, "__final__": True}


async def list_sections(ctx: AgentContext, _args: dict) -> dict:
    async with session_factory()() as db:
        document = await _document(db, ctx.tender_id)
        rows = await _sections(db, document)
    return {
        "items": [
            {
                "id": row.id,
                "heading": row.heading,
                "position": row.position,
                "status": row.status,
                "targetWords": row.target_words,
                "chars": len(re.sub(r"\s+", "", row.body or "")),
            }
            for row in rows
        ]
    }


async def read_section(ctx: AgentContext, args: dict) -> dict:
    section_id = str(args.get("sectionId") or "")
    async with session_factory()() as db:
        row = await db.get(BidSection, section_id)
    if not row:
        return {"error": "章节不存在"}
    return {
        "id": row.id,
        "heading": row.heading,
        "intent": row.intent,
        "targetWords": row.target_words,
        "body": row.body or "",
    }


async def audit_document(ctx: AgentContext, _args: dict) -> dict:
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        facts = load_facts(tender)
        document = await _document(db, ctx.tender_id)
        rows = await _sections(db, document)
    template = ((facts or {}).get("template") or {}).get("chapters") or []
    report = run_audit(tender, facts, rows, template)
    return {**report, "__final__": True}


TOOLS = {
    "tender_digest": ToolSpec(
        "tender_digest", "读取压缩后的招标要点（项目身份、资格、材料、参数、承诺、模板线索）", tender_digest
    ),
    "read_tender": ToolSpec("read_tender", "读取招标文件正文片段（args.limit 默认 8000 字符）", read_tender),
    "search_tender": ToolSpec("search_tender", "按关键词检索招标文件，返回命中行", search_tender),
    "read_analysis": ToolSpec("read_analysis", "读取招标解读结果（维度、评分、实质性要求、风险）", read_analysis),
    "build_analysis": ToolSpec("build_analysis", "生成并保存招标解读报告（总览/维度/评分/要求/风险）", build_analysis),
    "read_facts": ToolSpec("read_facts", "读取已抽取的招标事实", read_facts),
    "save_facts": ToolSpec("save_facts", "保存结构化的招标事实 JSON", save_facts),
    "detect_mode": ToolSpec("detect_mode", "判断应使用通用 / DOCX 模板 / PDF 模板生成", detect_mode),
    "extract_template": ToolSpec("extract_template", "抽取 DOCX/PDF/文本中的模板章节", extract_template),
    "save_outline": ToolSpec("save_outline", "按指定模式与目标字数生成并保存大纲（args: mode,targetWords）", save_outline),
    "list_sections": ToolSpec("list_sections", "列出当前标书章节及完成状态", list_sections),
    "read_section": ToolSpec("read_section", "读取单个章节正文（args: sectionId）", read_section),
    "write_book": ToolSpec("write_book", "按大纲逐节写作并做全书后处理（args: mode=missing/retry/all）", write_book),
    "write_section": ToolSpec("write_section", "重写指定章节（args: sectionId）", write_section),
    "audit_document": ToolSpec("audit_document", "按招标事实核对成稿并返回问题清单", audit_document),
    "save_review": ToolSpec("save_review", "把本次核对保存成一次质量检查（args: mode=rule/ai/mixed）", save_review),
    "save_fix": ToolSpec("save_fix", "为检查问题生成可应用的修订建议（args: reviewId,issueId）", save_fix),
}
