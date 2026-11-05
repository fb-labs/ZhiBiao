from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.api.analysis import current_analysis, serialize as serialize_analysis
from zhibiao.api.documents import document_of
from zhibiao.corpus import analysis_brief, document_text
from zhibiao.database import get_db
from zhibiao.domain import ReviewMode, Severity, TenderStatus
from zhibiao.llm import get_chat_client
from zhibiao.models import (
    Account,
    AuditEvent,
    BidSection,
    FixProposal,
    Review,
    ReviewIssue,
    Tender,
)
from zhibiao.prompts import fix_messages, review_messages
from zhibiao.security import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenders", tags=["v2-reviews"])
detail_router = APIRouter(prefix="/reviews", tags=["v2-reviews"])

PENALTY = {
    Severity.BLOCKER: 40,
    Severity.CRITICAL: 25,
    Severity.MAJOR: 12,
    Severity.MINOR: 5,
    Severity.INFO: 0,
}
SEVERITIES = set(PENALTY)
KEY_CHECKS = (
    ("营业执照", "资格文件", "投标文件未体现营业执照等主体资格材料，评标时可能被认定为资格不符。"),
    ("业绩", "类似业绩", "未检索到类似项目业绩描述，商务评分可能失分。"),
    ("保证金", "投标保证金", "未说明投标保证金缴纳安排，需核对招标文件的金额与形式要求。"),
)


class ReviewCreate(BaseModel):
    mode: str = Field(default=ReviewMode.MIXED)


class IssuePatch(BaseModel):
    status: str


def serialize_issue(row: ReviewIssue) -> dict:
    return {
        "id": row.id,
        "reviewId": row.review_id,
        "code": row.code,
        "severity": row.severity,
        "category": row.category,
        "location": row.location,
        "finding": row.finding,
        "recommendation": row.recommendation,
        "status": row.status,
    }


def serialize_review(row: Review, issues: list[ReviewIssue] | None = None) -> dict:
    data = {
        "id": row.id,
        "tenderId": row.tender_id,
        "documentId": row.document_id,
        "mode": row.mode,
        "status": row.status,
        "score": row.score,
        "verdict": row.verdict,
        "summary": row.summary,
        "payload": row.payload or {},
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }
    if issues is not None:
        data["issues"] = [serialize_issue(item) for item in issues]
    return data


def _rule_checks(
    tender: Tender, sections: list[BidSection], analysis, full_text: str
) -> list[dict]:
    issues: list[dict] = []
    if not any((row.body or "").strip() for row in sections):
        issues.append(
            {
                "code": "R001",
                "severity": Severity.BLOCKER,
                "category": "完整性",
                "location": "全文",
                "finding": "全部章节均未编写正文，投标文件无法提交。",
                "recommendation": "先在编写工作区逐章生成或手工补写正文。",
            }
        )
    empty_rows = [row for row in sections if not (row.body or "").strip()]
    for row in empty_rows[:8]:
        issues.append(
            {
                "code": "R002",
                "severity": Severity.CRITICAL,
                "category": "完整性",
                "location": row.heading,
                "finding": "该章节正文为空。",
                "recommendation": "补齐章节内容，确保对应评分项有实质响应。",
            }
        )
    for row in sections:
        body = (row.body or "").strip()
        if not body:
            continue
        words = len(body.replace(" ", "").replace("\n", ""))
        if row.target_words and words < int(row.target_words * 0.4):
            issues.append(
                {
                    "code": "R003",
                    "severity": Severity.MINOR,
                    "category": "表达",
                    "location": row.heading,
                    "finding": f"正文字数约 {words} 字，明显低于计划 {row.target_words} 字。",
                    "recommendation": "补充实施方法、进度安排与量化承诺。",
                }
            )
    if full_text:
        for keyword, category, finding in KEY_CHECKS:
            if keyword not in full_text:
                issues.append(
                    {
                        "code": "R004",
                        "severity": Severity.MAJOR,
                        "category": "响应性",
                        "location": category,
                        "finding": finding,
                        "recommendation": f"补充与“{keyword}”相关的证明材料或承诺条款。",
                    }
                )
    deadline = (tender.deadline or "").strip()
    if deadline:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
            try:
                parsed = datetime.strptime(deadline[:10].replace("年", "-").replace("月", "-").replace("日", ""), fmt)
            except ValueError:
                continue
            if parsed.date() < date.today():
                issues.append(
                    {
                        "code": "R005",
                        "severity": Severity.BLOCKER,
                        "category": "合规性",
                        "location": "投标截止",
                        "finding": f"投标截止时间 {deadline} 已过期。",
                        "recommendation": "核对最新时间安排，必要时放弃本次投标。",
                    }
                )
            break
    return issues


async def _load_review(db: AsyncSession, review_id: str) -> tuple[Review, list[ReviewIssue]]:
    review = await db.get(Review, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="检查记录不存在")
    issues = (
        await db.execute(select(ReviewIssue).where(ReviewIssue.review_id == review_id))
    ).scalars().all()
    return review, list(issues)


async def run_review_inline(db: AsyncSession, tender: Tender, mode: str, actor: str) -> Review:
    """质量检查的确定实现：规则检查 + AI 评标 + 事实核对，落 Review/Issue。

    核对 Agent 的 save_review 工具与端点兜底都调用这里。
    """
    if mode not in {ReviewMode.RULE, ReviewMode.AI, ReviewMode.MIXED}:
        raise HTTPException(status_code=400, detail="非法检查模式")
    tender_id = tender.id
    document = await document_of(db, tender_id)
    if not document:
        raise HTTPException(status_code=400, detail="请先生成标书大纲")
    sections = (
        await db.execute(
            select(BidSection)
            .where(BidSection.document_id == document.id)
            .order_by(BidSection.position.asc())
        )
    ).scalars().all()
    analysis = await current_analysis(db, tender_id)
    full_text = document_text(list(sections))

    issues: list[dict] = []
    ai_score: float | None = None
    ai_summary = ""
    ai_verdict = ""

    if mode in {ReviewMode.RULE, ReviewMode.MIXED}:
        issues.extend(_rule_checks(tender, list(sections), analysis, full_text))

    if mode in {ReviewMode.AI, ReviewMode.MIXED} and full_text:
        client = await get_chat_client(assistant="reviewer")
        try:
            verdict = await client.json_chat(
                review_messages(tender.title, analysis_brief(analysis), full_text),
                max_tokens=5000,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI 检查失败: %s", exc)
            if payload.mode == ReviewMode.AI:
                raise HTTPException(status_code=502, detail=f"模型调用失败: {exc}") from exc
            verdict = {}
        for entry in (verdict.get("issues") or [])[:40]:
            if not isinstance(entry, dict):
                continue
            severity = str(entry.get("severity") or Severity.MINOR).lower()
            if severity not in SEVERITIES:
                severity = Severity.MINOR
            issues.append(
                {
                    "code": "A001",
                    "severity": severity,
                    "category": str(entry.get("category") or "响应性").strip(),
                    "location": str(entry.get("location") or "").strip(),
                    "finding": str(entry.get("finding") or "").strip(),
                    "recommendation": str(entry.get("recommendation") or "").strip(),
                }
            )
        try:
            ai_score = float(verdict.get("score"))
        except (TypeError, ValueError):
            ai_score = None
        ai_summary = str(verdict.get("summary") or "").strip()
        ai_verdict = str(verdict.get("verdict") or "").strip()

    penalty = sum(PENALTY.get(item["severity"], 0) for item in issues if item["severity"] != Severity.INFO)
    rule_score = max(0.0, 100.0 - penalty)
    score = ai_score if ai_score is not None else rule_score
    if not issues:
        verdict_label = "可通过"
    elif score >= 85:
        verdict_label = "可通过"
    elif score >= 60:
        verdict_label = "需修改"
    else:
        verdict_label = "不可提交"
    verdict_label = ai_verdict or verdict_label
    summary = ai_summary or (
        f"共发现 {len(issues)} 项问题，其中阻断级 {sum(1 for i in issues if i['severity'] == Severity.BLOCKER)} 项。"
        if issues
        else "未发现明显问题，可按流程交付。"
    )

    review = Review(
        tender_id=tender_id,
        document_id=document.id,
        mode=mode,
        status="complete",
        score=round(score, 1),
        verdict=verdict_label,
        summary=summary,
        payload={
            "issueCount": len(issues),
            "ruleScore": rule_score,
            "aiUsed": ai_score is not None,
        },
    )
    db.add(review)
    await db.flush()
    for index, item in enumerate(issues, start=1):
        db.add(
            ReviewIssue(
                review_id=review.id,
                code=item["code"] or f"I{index:03d}",
                severity=item["severity"],
                category=item["category"],
                location=item["location"][:255],
                finding=item["finding"],
                recommendation=item["recommendation"],
            )
        )
    if tender.status in {TenderStatus.OUTLINED, TenderStatus.DRAFTING, TenderStatus.REVIEW}:
        tender.status = TenderStatus.REVIEW
    db.add(
        AuditEvent(
            actor=actor,
            action="review.run",
            target_type="tender",
            target_id=tender_id,
            detail={"mode": mode, "issues": len(issues), "score": review.score},
        )
    )
    await db.commit()
    return review


@router.post("/{tender_id}/reviews")
async def run_review(
    tender_id: str,
    payload: ReviewCreate,
    account: Account = Depends(require_permission("review.run")),
    db: AsyncSession = Depends(get_db),
):
    if payload.mode not in {ReviewMode.RULE, ReviewMode.AI, ReviewMode.MIXED}:
        raise HTTPException(status_code=400, detail="非法检查模式")
    tender = await db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(status_code=404, detail="项目不存在")
    document = await document_of(db, tender_id)
    if not document:
        raise HTTPException(status_code=400, detail="请先生成标书大纲")

    # 这一步的执行者是核对 Agent：读事实 → 逐项核对 → 落问题清单。
    from zhibiao.agents.steps import run_step_agent

    actor = account.username
    await run_step_agent(
        "auditor",
        tender_id=tender_id,
        task="按招标事实核对成稿，输出问题清单并按当前检查模式保存成一次质量检查。",
        payload={"mode": payload.mode},
        account=actor,
    )
    db.expire_all()
    review = await db.scalar(
        select(Review)
        .where(Review.tender_id == tender_id)
        .order_by(Review.created_at.desc())
        .limit(1)
    )
    if review is None:
        review = await run_review_inline(db, await db.get(Tender, tender_id), payload.mode, actor)
    _, rows = await _load_review(db, review.id)
    return serialize_review(review, rows)


@router.get("/{tender_id}/reviews")
async def list_reviews(
    tender_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Review).where(Review.tender_id == tender_id).order_by(Review.created_at.desc())
        )
    ).scalars().all()
    return {"items": [serialize_review(row) for row in rows]}


@detail_router.get("/{review_id}")
async def get_review(
    review_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    review, issues = await _load_review(db, review_id)
    return serialize_review(review, issues)


@detail_router.patch("/{review_id}/issues/{issue_id}")
async def patch_issue(
    review_id: str,
    issue_id: str,
    payload: IssuePatch,
    _: Account = Depends(require_permission("review.run")),
    db: AsyncSession = Depends(get_db),
):
    if payload.status not in {"open", "resolved", "ignored"}:
        raise HTTPException(status_code=400, detail="非法状态")
    issue = await db.get(ReviewIssue, issue_id)
    if not issue or issue.review_id != review_id:
        raise HTTPException(status_code=404, detail="问题不存在")
    issue.status = payload.status
    await db.flush()
    return serialize_issue(issue)


async def _section_for(db: AsyncSession, review: Review, issue: ReviewIssue) -> BidSection | None:
    if not review.document_id:
        return None
    rows = (
        await db.execute(
            select(BidSection).where(BidSection.document_id == review.document_id)
        )
    ).scalars().all()
    location = (issue.location or "").strip()
    for row in rows:
        if location and (location in row.heading or row.heading in location):
            return row
    return None


async def propose_fix_inline(db: AsyncSession, review: Review, issue: ReviewIssue) -> FixProposal:
    """修订建议的确定实现：按问题改一章正文并生成可应用的 FixProposal。

    修复 Agent 的 save_fix 工具与端点兜底都调用这里。
    """
    section = await _section_for(db, review, issue)
    if not section:
        raise HTTPException(status_code=400, detail="未找到可修改的章节")
    if not (section.body or "").strip():
        raise HTTPException(status_code=400, detail="该章节正文为空，请先生成或手工补写")

    client = await get_chat_client(assistant="reviser")
    try:
        revised = await client.chat(
            fix_messages(section.heading, issue.finding, issue.recommendation, section.body),
            max_tokens=4000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("修订建议生成失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"模型调用失败: {exc}") from exc
    revised = revised.strip()
    if not revised:
        raise HTTPException(status_code=502, detail="模型未返回修订内容")

    proposal = FixProposal(
        issue_id=issue.id,
        section_id=section.id,
        original_text=section.body,
        proposed_text=revised,
    )
    db.add(proposal)
    await db.commit()
    return proposal


def serialize_proposal(proposal: FixProposal, section: BidSection) -> dict:
    return {
        "id": proposal.id,
        "issueId": proposal.issue_id,
        "sectionId": proposal.section_id,
        "sectionHeading": section.heading,
        "originalText": proposal.original_text,
        "proposedText": proposal.proposed_text,
        "applied": proposal.applied,
        "createdAt": proposal.created_at.isoformat() if proposal.created_at else None,
    }


@detail_router.post("/{review_id}/issues/{issue_id}/fix")
async def propose_fix(
    review_id: str,
    issue_id: str,
    account: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    review, _ = await _load_review(db, review_id)
    issue = await db.get(ReviewIssue, issue_id)
    if not issue or issue.review_id != review_id:
        raise HTTPException(status_code=404, detail="问题不存在")

    # 这一步的执行者是修复 Agent：按问题定位章节并改写。
    from zhibiao.agents.steps import run_step_agent

    actor = account.username
    await run_step_agent(
        "fixer",
        tender_id=review.tender_id,
        task="针对指定检查问题改写对应章节，生成可应用的修订建议。",
        payload={
            "reviewId": review_id,
            "issueId": issue_id,
            "finding": issue.finding,
            "recommendation": issue.recommendation,
        },
        account=actor,
    )
    db.expire_all()
    proposal = await db.scalar(
        select(FixProposal)
        .where(FixProposal.issue_id == issue_id)
        .order_by(FixProposal.created_at.desc())
        .limit(1)
    )
    if proposal is None:
        review, _ = await _load_review(db, review_id)
        issue = await db.get(ReviewIssue, issue_id)
        proposal = await propose_fix_inline(db, review, issue)
    section = await db.get(BidSection, proposal.section_id) if proposal.section_id else None
    if not section:
        raise HTTPException(status_code=404, detail="章节不存在")
    return serialize_proposal(proposal, section)


@detail_router.post("/{review_id}/issues/{issue_id}/fix/{proposal_id}/apply")
async def apply_fix(
    review_id: str,
    issue_id: str,
    proposal_id: str,
    _: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    issue = await db.get(ReviewIssue, issue_id)
    proposal = await db.get(FixProposal, proposal_id)
    if not issue or not proposal or issue.review_id != review_id or proposal.issue_id != issue_id:
        raise HTTPException(status_code=404, detail="修订建议不存在")
    section = await db.get(BidSection, proposal.section_id) if proposal.section_id else None
    if not section:
        raise HTTPException(status_code=404, detail="章节不存在")
    section.body = proposal.proposed_text
    section.status = "written"
    proposal.applied = True
    issue.status = "resolved"
    await db.flush()
    return {"ok": True, "sectionId": section.id, "body": section.body}
