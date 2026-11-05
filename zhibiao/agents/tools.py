from __future__ import annotations

import re

from sqlalchemy import delete, select, update

from zhibiao.agents.audit import run_audit
from zhibiao.agents.base import AgentContext, ToolSpec
from zhibiao.agents.facts import assign_facts, load_facts, normalize_facts
from zhibiao.corpus import tender_corpus
from zhibiao.database import session_factory
from zhibiao.domain import TenderStatus
from zhibiao.generation.quality import run_quality
from zhibiao.generation.outline import assign_targets, build_outline as build_plan, match_scores
from zhibiao.generation.writer import generate_body
from zhibiao.llm import get_chat_client
from zhibiao.models import (
    Analysis,
    BidDocument,
    BidSection,
    Outline,
    Tender,
    TenderFile,
)
from zhibiao.prompts import fix_messages


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


async def build_outline(ctx: AgentContext, args: dict) -> dict:
    mode = str(args.get("mode") or "generic")
    target_words = int(args.get("targetWords") or 80000)
    planned_mode = (
        "tender" if mode in {"docx_template", "pdf_template", "text_template"} else "standard"
    )
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        if not tender:
            return {"error": "项目不存在"}
        corpus = await tender_corpus(db, tender, limit=60000)
        plans, note = build_plan(planned_mode, corpus)
        analysis = await _analysis(db, ctx.tender_id)
        score_items = ((analysis.scoring or {}).get("items") or []) if analysis else []
        match_scores(plans, score_items)
        assign_targets(plans, target_words)

        document = await _document(db, tender.id)
        if not document:
            document = BidDocument(tender_id=tender.id, title=f"{tender.title} 投标文件", status="draft")
            db.add(document)
            await db.flush()
        else:
            await db.execute(delete(BidSection).where(BidSection.document_id == document.id))
        position = 0
        parent_key = None
        for index, plan in enumerate(plans, start=1):
            if plan.level == 1:
                parent_key = f"s{index}"
            db.add(
                BidSection(
                    document_id=document.id,
                    position=position,
                    parent_key=None if plan.level == 1 else parent_key,
                    heading=plan.heading,
                    intent=plan.intent,
                    target_words=plan.target_words,
                )
            )
            position += 1
        await db.execute(
            update(Outline)
            .where(Outline.tender_id == tender.id, Outline.is_current.is_(True))
            .values(is_current=False)
        )
        latest = await db.scalar(
            select(Outline.revision)
            .where(Outline.tender_id == tender.id)
            .order_by(Outline.revision.desc())
        )
        db.add(
            Outline(
                tender_id=tender.id,
                revision=(latest or 0) + 1,
                sections={
                    "mode": mode,
                    "note": note,
                    "targetWords": target_words,
                    "plans": [plan.as_dict() for plan in plans],
                },
                is_current=True,
            )
        )
        if tender.status in {TenderStatus.INTAKE, TenderStatus.ANALYZED, TenderStatus.OUTLINED}:
            tender.status = TenderStatus.OUTLINED
        await db.commit()
    return {"mode": mode, "note": note, "sections": len(plans), "targetWords": target_words}


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


async def generate_missing(ctx: AgentContext, args: dict) -> dict:
    mode = str(args.get("mode") or "missing")
    async with session_factory()() as db:
        tender = await _tender(db, ctx)
        document = await _document(db, ctx.tender_id)
        analysis = await _analysis(db, ctx.tender_id)
        rows = await _sections(db, document)
        if not tender or not document:
            return {"error": "请先建立标书大纲"}
        selected = [
            row
            for row in rows
            if mode == "all" or not (row.body or "").strip() or row.status == "failed"
        ]
        tender_obj = tender
    if not selected:
        return {"generated": 0, "failed": 0, "message": "没有需要生成的章节"}
    client = await get_chat_client(assistant="writer")
    generated = 0
    failed = 0
    errors = []
    for index, section in enumerate(selected, start=1):
        await ctx.progress(
            int(index / len(selected) * 90),
            f"正文编制 Agent · {index}/{len(selected)} {section.heading}",
        )
        try:
            text = await generate_body(client, tender_obj, analysis, section, rows)
            if not text.strip():
                raise RuntimeError("模型未返回内容")
            async with session_factory()() as db:
                row = await db.get(BidSection, section.id)
                tender_row = await db.get(Tender, ctx.tender_id)
                if row:
                    row.body = text
                    row.status = "written"
                if tender_row and tender_row.status in {TenderStatus.OUTLINED, TenderStatus.DRAFTING}:
                    tender_row.status = TenderStatus.DRAFTING
                await db.commit()
            section.body = text
            section.status = "written"
            generated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append({"sectionId": section.id, "heading": section.heading, "error": str(exc)})
            async with session_factory()() as db:
                row = await db.get(BidSection, section.id)
                if row:
                    row.status = "failed"
                    await db.commit()
    async with session_factory()() as db:
        document = await _document(db, ctx.tender_id)
        rows = await _sections(db, document)
    quality = run_quality(rows)
    if quality["updates"]:
        async with session_factory()() as db:
            for section_id, body in quality["updates"].items():
                row = await db.get(BidSection, section_id)
                if row:
                    row.body = body
            await db.commit()
    return {
        "generated": generated,
        "failed": failed,
        "errors": errors[:10],
        "quality": quality["stats"],
        "issues": quality["issues"][:20],
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


async def fix_section(ctx: AgentContext, args: dict) -> dict:
    section_id = str(args.get("sectionId") or "")
    finding = str(args.get("finding") or args.get("message") or "")
    recommendation = str(args.get("recommendation") or "按招标文件要求补齐并量化响应")
    async with session_factory()() as db:
        row = await db.get(BidSection, section_id)
    if not row:
        return {"error": "章节不存在"}
    client = await get_chat_client(assistant="reviser")
    revised = await client.chat(
        fix_messages(row.heading, finding, recommendation, row.body or ""),
        max_tokens=4000,
    )
    revised = revised.strip()
    if not revised:
        return {"error": "模型未返回修订内容"}
    async with session_factory()() as db:
        row = await db.get(BidSection, section_id)
        if row:
            row.body = revised
            row.status = "written"
            await db.commit()
    return {"sectionId": section_id, "heading": row.heading, "chars": len(revised)}


TOOLS = {
    "tender_digest": ToolSpec(
        "tender_digest", "读取压缩后的招标要点（项目身份、资格、材料、参数、承诺、模板线索）", tender_digest
    ),
    "read_tender": ToolSpec("read_tender", "读取招标文件正文片段（args.limit 默认 8000 字符）", read_tender),
    "search_tender": ToolSpec("search_tender", "按关键词检索招标文件，返回命中行", search_tender),
    "read_analysis": ToolSpec("read_analysis", "读取招标解读结果（维度、评分、实质性要求、风险）", read_analysis),
    "read_facts": ToolSpec("read_facts", "读取已抽取的招标事实", read_facts),
    "save_facts": ToolSpec("save_facts", "保存结构化的招标事实 JSON", save_facts),
    "detect_mode": ToolSpec("detect_mode", "判断应使用通用 / DOCX 模板 / PDF 模板生成", detect_mode),
    "extract_template": ToolSpec("extract_template", "抽取 DOCX/PDF/文本中的模板章节", extract_template),
    "build_outline": ToolSpec("build_outline", "按指定模式与目标字数生成大纲（args: mode,targetWords）", build_outline),
    "list_sections": ToolSpec("list_sections", "列出当前标书章节及完成状态", list_sections),
    "read_section": ToolSpec("read_section", "读取单个章节正文（args: sectionId）", read_section),
    "generate_missing": ToolSpec("generate_missing", "生成未完成或全部章节（args: mode=missing/all）", generate_missing),
    "audit_document": ToolSpec("audit_document", "按招标事实核对成稿并返回问题清单", audit_document),
    "fix_section": ToolSpec("fix_section", "按问题改写指定章节（args: sectionId,finding,recommendation）", fix_section),
}
