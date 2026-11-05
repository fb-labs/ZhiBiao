"""Server-side resumable body generation for the B/S application."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable

from zhibiao.core.chapter_rules import (
    section_numbers,
    estimate_word_budgets,
    material_insert_placeholder,
    safe_structured_draft,
    shape_chapters,
    normalize_inline_numbering,
    container_chapter_numbers,
    strip_inline_numbering,
)
from zhibiao.core.legal_forms import legal_form_body
from zhibiao.core.format_forms import format_chapter_text_ex, validate_format_chapter
from zhibiao.core.credit_proofs import credit_proof_body, ensure_credit_channels
from zhibiao.core.pricing_notes import add_drum_pricing_note
from zhibiao.core.text_hygiene import hygienic_text, suppress_agency_assumptions
from zhibiao.core.consistency import (
    conflicts_from_facts,
    extract_chapter_facts,
    repair_summary,
    scan_cross_chapter_conflicts,
    scan_document_scope,
)
from zhibiao.core.content_plan import PLAN_SYSTEM, fallback_focus, parse_plans, planner_prompt
from zhibiao.core.ledger import build_project_ledger, facts_ready, truncate_ledger
from zhibiao.core.subject import industry_guard_text, classify_subject
from zhibiao.core.source_fidelity import (render_primary_contract, source_fidelity_issues,
                                      incomplete_tail)
from zhibiao.core.output_guard import find_forbidden_bid_terms, repair_prompt
from zhibiao.core.param_lock import build_param_lock, extract_device_registry, render_device_parameters
from zhibiao.core.commercial_terms import (check_commercial_consistency, extract_commercial_terms,
                                       neutralize_owner_placeholders, render_commercial_context)
from zhibiao.core.tail_repair import (append_incomplete_note, apply_tail_fragment,
                                  incomplete_source_note,
                                  tail_prompt_for, tail_problem, tail_repair_prompt)
from zhibiao.core.bill_of_materials import render_bill_context
from zhibiao.core.spec_clauses import render_clause_context, section_for
from zhibiao.core.chapter_quality import (is_tail_truncation_issue, quality_repair_prompt,
                                      validate_chapter_content)
from zhibiao.core.image_slots import normalize_image_placeholders
from zhibiao.core.text_normalize import normalize_generated_text
from zhibiao.core.tender_source import tender_pdf_bytes
from zhibiao.core.structured_forms import parse_pdf_tables, structure_from_report
from zhibiao.core.tender_checkup import format_chapter, format_sections
from zhibiao.core.requirements import build_requirement_inventory, select_requirement_context
from zhibiao.core.generation_context import chapter_source_context

# 台账并不等于完整招标原文；每章另行选择全文源要求，且续写/扩写/修订共用同一证据包。
WRITER_LEDGER_BUDGET_CHARS = 24000

# 共享消息（`_shared_ledger`）刻意保留**完整**台账：它是每章都能看到的「全书统一事实」，
# 靠裁剪省 token 会把末尾的【技术规格书要点】整节丢掉（这正是当初引入小节配额的原因）。
# 但完全不加上限也不行：超长招标文件（数百页）会把台账顶到几十万字，每章请求都带上它，
# 极易撞上服务商上下文上限而导致整篇生成失败。这里只设一个「病态上限」兜底，
# 正常规模（实测约 5 万字）不会被裁剪，语义与既有测试保持一致。
SHARED_LEDGER_MAX_CHARS = 60000
_LEDGER_OVERSIZE_MARK = "\n[台账超长，已按小节配额压缩展示；完整台账见项目解读结果]"

logger = logging.getLogger("zhinbiao2.generation")
_TASKS: dict[str, asyncio.Task[Any]] = {}

# 篇幅上限倍率：正文超过「目标字数 × 该倍率」就触发收敛改写。
# 质量修订/风险修订都会重发整章，实测会把篇幅越修越长，所以上限必须在落库前兜住。
WORD_LIMIT_RATIO = 1.5
# 仅超出目标几百字就改写没有意义（还会白白消耗一次模型调用）：还要求至少多出这么多字。
WORD_LIMIT_SLACK = 600
# 收敛改写最多尝试几次；每轮只有确实更短且仍通过质检/合规校验才会采用。
WORD_TRIM_ROUNDS = 2
# 收尾补全只补最后一句/最后一个表格行，额度必须小：给大了模型会借机把整章又写一遍
# （实测「适当扩写」一次能写出 1.9 万字，见 _chapter_llm 的注释）。
TAIL_REPAIR_MAX_TOKENS = 600
# 收尾续写最多几轮：表格缺行可能要续两三次（一次 600 token 约 400 字），
# 但绝不能无限续下去 —— 每轮结束都重新判「还缺不缺」，补够了立刻停。
TAIL_CONTINUE_ROUNDS = 3
# 单轮续写最多允许追加多少字（去空白）：超过就判「模型在重写整章」，保留原稿。
TAIL_CONTINUE_MAX_GROWTH = 1200
# 空章判据：只有**正文为空或几乎为空**才算「生成失败」（与 tail_repair.needs_repair 同阈值）。
# 断尾、表格缺行一律不算 —— 模型已经写出的实质正文必须落库，靠续写 + chapter_source 说明
# 降级；线上项目 bee64b91… 的 seq=14 就是被「末句缺句号」判失败、content 落成 0 的。
MIN_CHAPTER_BODY_CHARS = 20
_STOP_EVENTS: dict[str, asyncio.Event] = {}


def get_task(run_id: str) -> asyncio.Task[Any] | None:
    return _TASKS.get(run_id)


def launch(
    run_id: str,
    worker: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> asyncio.Task[Any]:
    stop_event = asyncio.Event()
    _STOP_EVENTS[run_id] = stop_event
    task = asyncio.create_task(worker(*args, stop_event, **kwargs), name=f"body-generation-{run_id}")
    _TASKS[run_id] = task
    return task


def request_stop(run_id: str) -> None:
    event = _STOP_EVENTS.get(run_id)
    if event is not None:
        event.set()


def _json_value(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return value if value is not None else fallback


def _score_context(title: str, focus: str, scores: list[dict[str, Any]]) -> str:
    if not scores:
        return "（暂无已抽取评分项）"

    def grams(text: str) -> set[str]:
        clean = re.sub(r"[\W_]+", "", text or "")
        return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))}

    target_grams = grams(title + " " + focus)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in scores:
        label = str(item.get("name") or "")
        criteria = str(item.get("criteria") or "")
        ranked.append((len(target_grams & grams(label + " " + criteria)), item))
    ranked.sort(key=lambda row: row[0], reverse=True)
    selected = [item for score, item in ranked[:8] if score > 0]
    if not selected:
        selected = [item for _, item in ranked[:5]]
    lines: list[str] = []
    for item in selected:
        line = f"- {item.get('name')}（{item.get('score')}分）"
        if item.get("part"):
            line += f"；所属部分：{item['part']}"
        if item.get("criteria"):
            line += f"；评分标准：{str(item['criteria'])[:300]}"
        if item.get("source"):
            line += f"；出处：{str(item['source'])[:120]}"
        lines.append(line)
    return "\n".join(lines) or "（暂无与本节匹配的评分项）"


def _chapter_scope(chapter: dict[str, Any]) -> str:
    siblings = chapter.get("siblings") or []
    sibling_text = "、".join(siblings[:12]) or "无"
    # 偏差/偏离类章节是典型的自伤：招标文件格式9/10 只要求「无偏差时在本页写无」，
    # 一份主动罗列偏离项的技术标，等于替评委把「重大偏差」条款（20.1④）摆到桌面上。
    title = str(chapter.get("title") or "")
    if re.search(r"偏离|偏差", title):
        return (
            "本节标题含「偏离/偏差」：只作**无偏离声明**与肯定性响应承诺（对参数、功能、工期、"
            "质保、付款方式逐项声明完全响应、无负偏离），不得罗列任何偏离条目、替代措施或"
            "「偏离项登记表」；制造过程中的质量缺陷、返修不得写成偏离项。"
        )
    if chapter.get("has_children"):
        children = "、".join(chapter.get("children") or []) or "下级章节"
        return (
            f"本节是分支总述，只写范围、总体思路和分工，简要说明由{children}分别展开；"
            "不要生成下级标题或重复下级措施。"
        )
    return (
        f"本节是具体执行章节，应围绕自身标题展开可核查的做法；同级章节"
        f"（{sibling_text}）承担各自内容，不在本节重复。"
    )


async def _set_plan(
    pool: Any,
    run_id: str,
    project_id: str,
    rows: list[dict[str, Any]],
    ledger_text: str,
    llm: dict[str, Any],
    configured: bool,
    call_llm: Callable[..., tuple[bool, str]],
    chapter_hint: Callable[[str], str],
) -> list[dict[str, Any]]:
    shaped = shape_chapters(rows)
    siblings: dict[str, list[str]] = {}
    for chapter in shaped:
        siblings.setdefault(str(chapter.get("parent_id") or ""), []).append(chapter["title"])
    for chapter in shaped:
        peers = siblings.get(str(chapter.get("parent_id") or ""), [])
        chapter["siblings"] = [title for title in peers if title != chapter["title"]]
    missing = [chapter for chapter in shaped if not str(chapter.get("writing_focus") or "").strip()]
    if not missing:
        async with pool.acquire() as conn:
            await conn.execute(
                "update project_generation_runs set status=case when status='stopping' then status else 'running' end, "
                "plan_done=$2, plan_total=$2, heartbeat_at=now(), message='已载入全书写作计划' where id::text=$1",
                run_id, len(shaped),
            )
        return shaped

    async with pool.acquire() as conn:
        await conn.execute(
            "update project_generation_runs set status=case when status='stopping' then status else 'planning' end, "
            "plan_done=0, plan_total=$2, current_title='全书章节编排', "
            "message='正在为全书分配章节写作重点', heartbeat_at=now() where id::text=$1",
            run_id, len(missing),
        )
    plans: dict[int, str] = {}
    if configured:
        try:
            planner_cfg = dict(llm)
            planner_cfg["max_tokens"] = max(2048, min(8192, int(llm.get("max_tokens") or 4096)))
            ok, output = await asyncio.to_thread(
                call_llm, planner_cfg, planner_prompt(shaped, ledger_text),
                int(llm.get("timeout", 300)), PLAN_SYSTEM,
            )
            if ok:
                plans = parse_plans(output)
            else:
                logger.warning("[正文编排] 计划生成失败，使用确定性章节分工: %s", output)
        except Exception as exc:
            logger.warning("[正文编排] 编排异常，使用确定性章节分工: %s", exc)

    async with pool.acquire() as conn:
        for chapter in missing:
            focus = plans.get(chapter["seq"]) or fallback_focus(
                chapter, chapter_hint(str(chapter.get("title") or ""))
            )
            chapter["writing_focus"] = focus
            await conn.execute(
                "update project_chapters set writing_focus=$2, updated_at=now() where id=$1",
                chapter["id"], focus,
            )
        await conn.execute(
            "update project_generation_runs set status=case when status='stopping' then status else 'running' end, "
            "plan_done=$2, plan_total=$2, current_seq=null, current_title=null, "
            "message='全书写作计划已保存，开始逐章生成', heartbeat_at=now() where id::text=$1",
            run_id, len(missing),
        )
    return shaped


# ── 截断续写（手法参考 80 端口 services/routers/generate.py::_continue_if_truncated）──
# 为什么必须做：模型把 max_tokens 写满时，正文往往已经有一两千字，旧逻辑只拦「不足 50 字」，
# 于是「写了一半的话」被判成功入库，导出到 Word 就是一句没写完的半截话。
# 注意：80 端口另有一条「content 为空就拿 reasoning_content 当正文」的兜底，**这里不采用**——
# 思考文本不是成稿，那正是它「一半内容不成形」的根因；3001 改为在调用层换姿势重试。
_CONT_SENT_TAIL = frozenset("。！？；：.!?;:）)】”\"'…—")
_CONT_PROMPT = (
    "上面是你刚刚写的正文末尾。请紧接着继续写下去：从被中断的那句话继续，"
    "保持相同的语言风格、层级结构与编号体例；不要重复已经写过的任何句子或段落，"
    "不要重新输出标题，不要写任何说明性文字，直接接着写正文。"
)


def _resolve_meta_llm(call_llm: Any) -> Any:
    """取「能回带 finish_reason」的模型调用函数；拿不到就返回 None（不续写）。

    生产环境 run_generation 注入的就是 app.pipeline_api.call_llm，此时可用它的 meta 版本
    判断是否被 max_tokens 截断；测试注入的是替身，则跳过续写，保持既有测试语义。
    """
    try:
        from zhibiao.llm import call_llm as _real, call_llm_meta as _meta

        return _meta if call_llm is _real else None
    except Exception:  # noqa: BLE001 - 拿不到就按「不续写」处理，绝不因此报错
        return None


def _chapter_llm(llm: dict[str, Any], target_words: int) -> dict[str, Any]:
    """把单次调用的 max_tokens 收紧到本节篇幅上限。

    为什么必须收：配置里的 max_tokens（实测 4096）是按单次调用给的上限，
    对目标只有两千字的章节来说等于允许模型一次写五千九百字。
    模型确实会写满，finish_reason=length 又触发续写，
    实测一章涨到 19370 字（目标的 9 倍）—— 这既是最贵的废 token 来源，
    也是参数串台的温床（三台相似设备写进同一段长文，初撑力/工作阻力必然混）。
    按「目标字数 × 上限比例」折算成 token（汉字约 1.45 字/token）并留余量。
    """
    target = int(target_words or 0)
    if target <= 0:
        return llm
    cap_chars = int(target * WORD_LIMIT_RATIO)
    cap_tokens = max(700, int(cap_chars / 1.45) + 200)
    cfg = dict(llm)
    configured = int(llm.get("max_tokens") or 4096)
    cfg["max_tokens"] = max(700, min(configured, cap_tokens))
    return cfg


def _looks_incomplete(text: str, finish_reason: str, target_words: int) -> bool:
    """判据（宁可少续不可乱续）：
    ① finish_reason == "length"：服务商明确表示被长度截断，最强信号；
    ② 正文不足目标字数 70% 且结尾不是句末标点：明显写一半停了。
    正常写短的章节不会被强行拉长。
    """
    body = str(text or "").strip()
    if len(body) < 50:
        return False
    if str(finish_reason or "") == "length":
        return True
    # A long chapter can still end mid-sentence. Length relative to the target
    # must not hide a strong unfinished tail such as “现场服务团队配备”.
    if incomplete_tail(body):
        return True
    # 明显短于目标：即便结尾有句号也要接着写。实测 413 字的「本章各部分分工如下」导语
    # 结尾是句号，被旧的「有句号就算写完」判据放过，最终整章只落库 413 字。
    target = int(target_words or 0)
    if target and len(body) < int(target * 0.5):
        return True
    if body[-1:] in _CONT_SENT_TAIL:
        return False
    return bool(target) and len(body) < int(target * 0.7)


class _KeepMissing(dict):
    """模板缺字段时保留 {xxx} 占位，而不是让整章生成抛 KeyError。"""

    def __missing__(self, key: Any) -> str:
        return "{%s}" % key


def _safe_format(template: str, fields: dict[str, Any]) -> str:
    """模板与调用点不同步时不能让整章失败：缺字段原样保留。"""
    try:
        return str(template).format_map(_KeepMissing(fields))
    except (IndexError, ValueError) as exc:  # pragma: no cover - 防御性
        logger.warning("[正文生成] 提示词模板渲染失败，本节改用未渲染模板：%s", exc)
        return str(template)


# ── 正文内嵌小标题：空标题与「编号只拼一次」──────────────────────────────────
# 真实事故（成稿 v3 第 4 章）：正文里出现一行只有编号、没有标题的小标题，旧链路把它
# 编号成「## 4.7」，导出侧再渲染成只有「4.7」的 Word 标题；模型随后在这个没有名字的
# 标题下面，把前面 4.3/4.4/4.5 已经写过的里程碑、人员分工、交付物清单又写了一遍 ——
# 空标题正是「同一组内容被重复渲染」的入口。同一份成稿里还有另一种编号病：章节标题是
# 「6.3.3 安全管理制度」，正文里的加粗小标题却是「6.3.1.3.1 安全生产责任制」，编号里
# 套着上一版的父链（6.3.1.3 再拼一个 .1），等于父段重复了一次。
# 落库前必须挡住这两类：
#   ① 空标题（只剩编号/标点/零宽字符）的内嵌小标题：删掉该行，其下级上提一层；
#   ② 带编号的加粗小标题（**6.3.1.3.1 安全生产责任制**）：编号按「本章节号 + 本地层级」
#      重新拼一次 —— 父链重接、父段去重，不会拼出 6.3.1.3.1 这种重复形式。
_FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff\u00ad"
_HEADING_LINE = re.compile(r"^[ \t]{0,3}(#{2,6})[ \t]*(.*?)[ \t]*$")
_BOLD_LINE = re.compile(r"^([ \t]*)\*\*(.+?)\*\*[ \t]*$")
_BOLD_NUMBERED = re.compile(r"^([ \t]*)\*\*[ \t]*(\d+(?:[.．]\d+)+)[ \t]+(\S.*?)[ \t]*\*\*[ \t]*$")
_LEAD_ARABIC = re.compile(r"^[（(]?[ \t]*\d+(?:[.．]\d+)*[ \t]*[)）.．、:：]?[ \t]*")
_HEADING_PUNCT = re.compile(
    r"[()（）\[\]【】<>《》\-—–_:：,，.。;；、/\\|\"'“”‘’*#`~!！?？+=%&…·]+")
_NUMERAL_ONLY = re.compile(r"^[\d一二三四五六七八九十百零两壹贰叁肆伍陆柒捌玖拾]+$")


def _visible_heading(text: str) -> str:
    """小标题里真正看得见的文字（去空白/零宽字符/编号/标点）；空串 = 空标题。"""
    value = str(text or "")
    for char in _INVISIBLE_CHARS:
        value = value.replace(char, "")
    value = re.sub(r"\s+", "", value)
    value = _LEAD_ARABIC.sub("", value, count=1)
    return _HEADING_PUNCT.sub("", value)


def _has_heading_title(text: str) -> bool:
    """这个标题有没有真正的文字（只剩编号、只剩标点都算没有）。"""
    visible = _visible_heading(text)
    return bool(visible) and not _NUMERAL_ONLY.match(visible)


def _drop_empty_subheadings(text: str) -> tuple[str, int]:
    """删掉没有标题的内嵌小标题行，并把它的下级整体上提一层。

    与编号归一同一套边界：代码围栏内不动（围栏只有成对出现才可信，未闭合时按普通行处理）。
    """
    body = str(text or "")
    if not body:
        return body, 0
    dropped: list[int] = []
    kept: list[str] = []
    changed = 0
    # 围栏只有成对出现才可信（与 chapter_rules._renumber_rows 同一口径）：未闭合的围栏
    # 不能把后半章都当成代码，否则空标题就漏过去了。
    track_fence = sum(1 for line in body.splitlines() if _FENCE_LINE.match(line)) % 2 == 0
    fence = ""
    for raw in body.splitlines(keepends=True):
        core = raw.rstrip("\r\n")
        ending = raw[len(core):]
        marker = _FENCE_LINE.match(core) if track_fence else None
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            kept.append(raw)
            continue
        if fence:
            kept.append(raw)
            continue
        heading = _HEADING_LINE.match(core)
        if heading:
            depth, title = len(heading.group(1)), heading.group(2)
            while dropped and depth <= dropped[-1]:
                dropped.pop()
            if not _has_heading_title(title):
                dropped.append(depth)
                changed += 1
                continue
            if dropped:  # 父标题被删掉了，这一行升到它的层级
                depth = max(2, depth - len(dropped))
                core = "#" * depth + " " + title.strip()
            kept.append(core + ending)
            continue
        bold = _BOLD_LINE.match(core)
        if bold and not _has_heading_title(bold.group(2)):
            changed += 1  # 加粗的「只有编号」行同样是空标题，删掉
            continue
        kept.append(core + ending)
    if not changed:
        return body, 0
    return "".join(kept), changed


def _strict_child_number(chapter_number: str, number: str) -> str:
    """把已有的小标题编号重接成「本章节号 + 本地层级」，父段重复只保留一次。

    合法：6.3.3 → 6.3.3.1、6.3.3.1.2；非法：6.3.3 下的 6.3.3.3.1 与 6.3.1.3.1，
    一律归一到 6.3.3.1。旧编号比本章号深时，前面那截是上一版（或上一层）的父链，
    目录重建后已经失效，只保留本地层级重接 —— 宁可重新起一层，也不留下父段重复的编号。
    """
    parent = [seg for seg in str(chapter_number or "").strip().split(".") if seg]
    if not parent:
        return str(number or "").strip()
    local = [seg for seg in re.split(r"[.．]", str(number or "")) if seg]
    if len(local) > len(parent):
        local = local[len(parent):]
    elif local[:len(parent)] == parent:
        local = local[len(parent):]
    # 本地层级里又重复了整条父链（a.b.c → a.b.c.a.b.c.d）也去掉
    while local and local[:len(parent)] == parent:
        local = local[len(parent):]
    while len(local) > 1 and local[0] == parent[-1]:
        local = local[1:]
    if not local:
        local = ["1"]
    return ".".join(parent + local)


def _rebase_bold_numbers(text: str, chapter_number: str) -> tuple[str, int]:
    """加粗小标题的编号只拼一次：父段重复去掉，子编号严格接在本章节号下一层。"""
    label = str(chapter_number or "").strip()
    body = str(text or "")
    if not label or not body:
        return text, 0
    changed = 0
    kept: list[str] = []
    for raw in body.splitlines(keepends=True):
        core = raw.rstrip("\r\n")
        ending = raw[len(core):]
        matched = _BOLD_NUMBERED.match(core)
        if not matched:
            kept.append(raw)
            continue
        indent = matched.group(1)
        old = matched.group(2).replace("．", ".")
        title = matched.group(3)
        new = _strict_child_number(label, old)
        if new == old:
            kept.append(raw)
            continue
        kept.append("%s**%s %s**%s" % (indent, new, title, ending))
        changed += 1
    if not changed:
        return text, 0
    return "".join(kept), changed


def _renumber_inline(text: str, number: str, containers: Any = frozenset()) -> tuple[str, int]:
    """正文内嵌小标题编号归一；容器章改为去编号（失败时原样返回，绝不影响正文生成）。

    编号归一之前先删掉「只有编号、没有标题」的小标题行，归一之后再把加粗小标题的编号
    重接一次：既保证不产出空标题节点，也保证子编号严格接在本章节号下一层、父段不重复。
    """
    try:
        text, dropped = _drop_empty_subheadings(text)
        label = str(number or "").strip()
        if label in (containers or ()):
            # 容器章（自身还有下级章节）的内嵌编号会与下级章号撞号，只保留标题文字
            fixed, _changed = strip_inline_numbering(text)
            return fixed, dropped
        fixed, changed = normalize_inline_numbering(text, label)
        fixed, rebased = _rebase_bold_numbers(fixed, label)
        return fixed, dropped + changed + rebased
    except Exception as exc:  # noqa: BLE001
        logger.warning("[编号归一] 失败：%s", exc)
        return text, 0


_ROOF_CLAUSE_FOCUS = re.compile(r"端尾|结构|组成|工作原理|参数|指标|对照")


def roof_completeness_scope(title: str, path: str = "", focus: str = "") -> bool:
    """本章是否适用「破顶装置尺寸必须写全」。

    只看目录路径与写作要点（写作前就能拿到的信息），不看正文 —— 因为这是决定
    「要不要注入破顶原文条款」和「要不要判尺寸缺失」的同一个开关：
    开关打开 = 依据已送进上下文，模型写不出才算漏项；开关关着就不判，
    避免「没给原文却要求写全」这种必败判定（线上 6 个失败章节全是这么来的）。
    """
    return "破顶" in " ".join([str(title or ""), str(path or ""), str(focus or "")])


def _format_source_label(meta: Any) -> str:
    """格式章节的「来源」文案：界面上要能一眼看出这一章是哪种还原路径。

    结构化 = 按 PDF 版式直出（列数/合并/合计位置/注释归属都来自版式）；
    合并 = 版式直出的表格 + 从原文正文补回来的表外文字（结构化只带表前后紧邻的几行，
    没有表格的小节会被掏空，所以变薄时走合并）；
    启发式 = 从纯文本猜列，也就是历史上错位的那个路径，必须把原因写出来。
    旧项目的原件字节当时没有落库，所以 no_pdf 要明确告诉用户"重新上传即可"。
    """
    detail = meta if isinstance(meta, dict) else {}
    source = str(detail.get("source") or "")
    if source == "structured":
        return "格式原文（结构化还原）"
    if source == "merged":
        # 表格按版式、表外正文按原文：两条路都用上了，不能显示成"降级"
        return "格式原文（结构化还原+原文正文合并）"
    reason = str(detail.get("reason") or "no_pdf")
    if reason == "no_pdf":
        return ("格式原文（启发式·降级:no_pdf — 原始 PDF 未留存，"
                "重新上传招标文件后可用结构化还原）")
    return "格式原文（启发式·降级:%s）" % reason


def _validate_chapter(text: str, title: str, registry: Any, source: str, *,
                      allow_material: bool = False, chapter_number: str = "",
                      commercial: dict[str, str] | None = None,
                      roof_in_scope: bool | None = None,
                      technical_evidence: list[dict] | None = None,
                      material_evidence: list[dict] | None = None) -> list[str]:
    """兼容包装：chapter_number 参数可能尚未部署，缺参数时退回旧行为。

    strict_completeness=True 只在**生成**时用：新写的正文必须把破顶装置尺寸、
    材质涂装等原文硬指标写全；存量正文导出时不套这条，避免把已经在用的项目
    一夜之间全部拦下（用户要的是"以后不再漏"，不是"今天全部导不出去"）。

    roof_in_scope 由调用方按本章目录路径/写作要点判定，并且在同一判断下已经把
    破顶装置原文条款注入写作上下文：只有「要求」与「依据」同时到场才判缺失。
    """
    try:
        issues = validate_chapter_content(
            text, title, registry, source,
            allow_material=allow_material, chapter_number=chapter_number,
            strict_completeness=True, roof_in_scope=roof_in_scope,
        )
    except TypeError:  # pragma: no cover - 兼容旧签名
        issues = validate_chapter_content(text, title, registry, source, allow_material=allow_material)
    if not allow_material:
        issues += source_fidelity_issues(text, title, source)
        from zhibiao.core.technical_evidence import technical_evidence_issues, quantity_breakdown_issues, material_evidence_issues, process_scope_issues
        issues += technical_evidence_issues(text, technical_evidence or [])
        issues += quantity_breakdown_issues(text)
        issues += material_evidence_issues(text, material_evidence or [])
        issues += process_scope_issues(text, source)
        # 商务口径（投标有效期/工期/质保期/预算）写反就是负偏离，直接废标。
        issues += check_commercial_consistency(commercial, text)
    return list(dict.fromkeys(issues))


def _blocking_quality_issues(issues: list[str]) -> list[str]:
    """质检项里真正会让整章作废的那些；「章末断尾」不在其中。

    断尾是**结构未完成**：落库前还有专门的收尾续写（`_finish_chapter_tail`）可以补，
    补不上也按「保留正文 + chapter_source 说明」降级。把它当内容错误直接清零整章，
    就等于「模型写了 800 字、只差一个句号 → 用户拿到 0 字」，正是线上 seq=14 的事故。
    """
    return [item for item in list(issues or []) if not is_tail_truncation_issue(item)]


def _trim_overlap(prev: str, addition: str) -> str:
    """去掉续写开头与已写内容重复的接头，避免同一段被写两遍。

    模型续写常见的重复粒度是**整段重述**，而不是 12 字以上字符级重叠，
    所以这里除了字面重叠（阈值降到 5 字）之外，再做一次整段去重。
    """
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


async def _continue_until_complete(
    meta_llm: Any, llm: dict[str, Any], prompt: str, system: str | None, shared: str | None,
    text: str, finish_reason: str, target_words: int, max_rounds: int = 2,
    state: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """被截断或明显没写完时接着写，直到写完或不再有有效增量。返回 (正文, 续写轮数)。

    `state`（可选）用于回带最后一次调用的 finish_reason：收尾判据要据此区分
    「模型明确在 max_tokens 上被切断（表格一定没写完）」和「自己写完了」。
    """
    out = str(text or "")
    fr = str(finish_reason or "")
    rounds = 0
    # 续写的目的是"补完被截断的句子"，不是"继续加长"。已经到篇幅上限就停，
    # 否则 finish_reason=length 会让它一轮接一轮写下去（实测一章涨到 19370 字）。
    ceiling = int(int(target_words or 0) * WORD_LIMIT_RATIO) if int(target_words or 0) > 0 else 0
    for _ in range(max(1, int(max_rounds))):
        if ceiling and len(re.sub(r"\s", "", out)) >= ceiling:
            logger.info("[正文生成] 正文已达篇幅上限（%d 字 ≥ %d），不再续写",
                        len(re.sub(r"\s", "", out)), ceiling)
            break
        if not _looks_incomplete(out, fr, target_words):
            break
        extra = [
            {"role": "assistant", "content": out[-1500:]},
            {"role": "user", "content": _CONT_PROMPT},
        ]
        try:
            ok, addition, meta = await asyncio.to_thread(
                meta_llm, llm, prompt, int(llm.get("timeout", 300)), system, shared, extra,
            )
        except Exception as exc:  # noqa: BLE001 - 续写失败绝不能丢掉已经写好的正文
            logger.warning("[正文生成] 续写调用异常，保留已写正文：%s", exc)
            break
        fr = str((meta or {}).get("finish_reason") or "")
        if not ok or not str(addition or "").strip():
            logger.warning("[正文生成] 续写未返回内容，保留已写正文（%d 字）", len(out))
            break
        piece = _trim_overlap(out, str(addition))
        if len(piece.strip()) < 20:
            logger.info("[正文生成] 续写无有效增量，停止续写（%d 字）", len(out))
            break
        out = out.rstrip() + "\n" + piece.lstrip()
        rounds += 1
    if rounds:
        logger.info("[正文生成] 截断续写 %d 轮，正文 %d 字", rounds, len(out))
    if state is not None:
        state["finish_reason"] = fr
        state["rounds"] = rounds
    return out, rounds


# 原文条目清单缓存：(tender_text 的对象标识, 文本长度, 章节标题) → 条目名。
# 判据与续写上下文都要用它，缓存只为省掉重复扫描整册招标原文（每章一次，48 章很可观）。
_SOURCE_ITEMS_CACHE: dict[tuple[int, int, str], list[str]] = {}


def _source_table_items(tender_text: str, hint: str) -> list[str]:
    """招标原文里与本章标题对得上的「逐条清单」条目名；拿不到就回空表（绝不猜）。

    只用于收尾判据与续写上下文：确认本章末表确实是原文同表的前几行之后，
    才把缺的条目名交给模型照着写 —— 既让它补得全，也避免它自己编条目名。
    取不到或对不上时判据仍可用（退回「表头在、表体只剩 1 行」这条纯结构判据）。
    """
    source = str(tender_text or "")
    if not source.strip():
        return []
    key = (id(source), len(source), str(hint or ""))
    if key in _SOURCE_ITEMS_CACHE:
        return _SOURCE_ITEMS_CACHE[key]
    items: list[str] = []
    try:
        from zhibiao.core.table_completeness import tender_requirement_items

        items = list(tender_requirement_items(source, hint=str(hint or "")) or [])
    except Exception as exc:  # noqa: BLE001 - 取不到原文清单不影响结构判据
        logger.warning("[收尾补全] 取原文同表条目失败，退回结构判据：%s", exc)
    if len(_SOURCE_ITEMS_CACHE) > 256:
        _SOURCE_ITEMS_CACHE.clear()
    _SOURCE_ITEMS_CACHE[key] = items
    return items


async def _finish_chapter_tail(
    text: str,
    *,
    title: str,
    call_llm: Callable[..., tuple[bool, str]],
    llm: dict[str, Any],
    writer_system: str | None,
    chapter_shared: str | None,
    expected_rows: int | None = None,
    source_items: list[str] | None = None,
    truncated: bool = False,
    stop_event: asyncio.Event | None = None,
    rounds: int = TAIL_CONTINUE_ROUNDS,
) -> tuple[str, list[str], bool, str]:
    """收尾处理：**先续写**，续写不可用才只记一条中性说明。返回 (正文, 记录, 是否未完成, 来源说明)。

    为什么不是「补最后一句」就完事：表格断在中途时缺的是**后面几行**，把最后一行重写一遍
    补不回 12~14 行；编号条目只写了 `3. 端尾` 时缺的是这一条的正文与后续各条。
    因此按断点类型分两种动作（判据与提示词都在 tail_repair）：
    * `replace`：末行本身写残（半句、半个表格行）—— 只重写这一个单元，不碰前面；
    * `append`：末行完整但后面还有内容（表格缺行、编号条目缺正文）—— 从断点往后续写。

    绝不伪造：模型没返回内容 / 只回 `<OK>` / 返回内容过长（在重写整章）时一律不动正文；
    补不齐就只记录事实，不改写、不编造，也不追加「综上所述」这类通用收尾句：
    * 表格断尾：沿用既有降级，表后追加 `（本节续写未完成，请人工补齐：…）` 一行说明；
    * 正文/编号条目断尾：**正文一个字都不改**（线上 seq=14 的 800 字必须原样落库），
      未完成的事实由第 4 个返回值写进 `chapter_source`（见 `incomplete_source_note`）。
    判据与追加都是幂等的，重复跑不会二次改写。
    """
    body = str(text or "")
    source_items = list(source_items or [])
    notes: list[str] = []
    incomplete = False
    source_note = ""
    continued = 0
    if not body.strip():
        return body, notes, incomplete, source_note
    for _round in range(max(1, int(rounds))):
        if stop_event is not None and stop_event.is_set():
            break
        # `truncated` 只在第一轮采信：它证明的是「这一次调用被 max_tokens 切断」，
        # 续写成功后正文已经不是原来那份输出了，再据此判残会把补好的章节又标成未完成。
        problem = tail_problem(body, expected_rows=expected_rows,
                               source_items=source_items,
                               truncated=bool(truncated and _round == 0))
        if problem is None:
            break
        prompt = tail_prompt_for(problem, title=title)
        try:
            ok, fragment = await asyncio.to_thread(
                call_llm, {**llm, "max_tokens": TAIL_REPAIR_MAX_TOKENS}, prompt,
                int(llm.get("timeout", 300)), writer_system, chapter_shared,
            )
        except Exception as exc:  # noqa: BLE001 - 收尾失败绝不能丢掉正文
            logger.warning("[收尾补全] 章节 %s 调用异常：%s", title, exc)
            notes.append("续写调用异常")
            break
        piece = str(fragment or "").strip()
        if not ok or not piece or piece.strip("<> 　").upper() == "OK":
            logger.warning("[收尾补全] 章节 %s 续写不可用（%s），正文原样保留、只记来源说明",
                           title, problem.get("reason"))
            notes.append("续写不可用")
            break
        candidate = apply_tail_fragment(body, problem, piece)
        growth = len(re.sub(r"\s", "", candidate)) - len(re.sub(r"\s", "", body))
        if candidate == body or growth <= 0:
            notes.append("续写无有效增量")
            break
        if growth > TAIL_CONTINUE_MAX_GROWTH:
            logger.warning("[收尾补全] 章节 %s 续写内容过长（+%d 字），保留原稿", title, growth)
            notes.append("续写内容过长")
            break
        body = candidate
        continued += 1
        notes.append("续写表格缺行" if str(problem.get("mode")) == "append" else "补全断点单元")
        logger.info("[收尾补全] 章节 %s 第 %d 轮已续写（%s）",
                    title, _round + 1, problem.get("reason"))
    # 收尾判定：真补过内容就只认结构证据（正文已不是原来那份被截断的输出了）；
    # 一轮都没补成时，「本次调用被 max_tokens 截断」这条证据仍然有效，据此标注待人工补齐。
    remaining = tail_problem(body, expected_rows=expected_rows, source_items=source_items,
                             truncated=bool(truncated and not continued))
    if remaining is not None:
        source_note = incomplete_source_note(remaining)
        incomplete = True
        notes.append("未完成，已标注人工补齐")
        logger.warning("[收尾补全] 章节 %s 仍不完整（%s），已记录到章节来源待人工补齐",
                       title, remaining.get("reason"))
    return body, notes, incomplete, source_note


def _chapter_numbers(planned: list[dict[str, Any]]) -> dict[str, str]:
    """章节层级编号（1 / 1.1 / 1.1.1），替代会被模型写进标题的全局 seq。"""
    return section_numbers([dict(item) for item in planned or []])


async def _heartbeat(pool: Any, run_id: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "update project_generation_runs set heartbeat_at=now() "
                        "where id::text=$1 and status in ('queued','planning','running','stopping')",
                        run_id,
                    )
            except Exception as exc:
                logger.warning("[正文生成] 心跳更新失败，任务继续运行: %s", exc)


async def run_generation(
    run_id: str,
    project_id: str,
    target_words: int,
    issues: list[str],
    pool: Any,
    llm: dict[str, Any],
    configured: bool,
    call_llm: Callable[..., tuple[bool, str]],
    chapter_hint: Callable[[str], str],
    writer_prompt: str,
    writer_system: str,
    owner: str,
    stop_event: asyncio.Event,
    *,
    chapter_ids: list[str] | None = None,
) -> None:
    """Generate body text for pending chapters.

    chapter_ids：只重跑这些章节（单项「重新生成本章」）。给定时全书其余章节的正文、
    写作重点与状态一律不动，编排也只针对这些章节，避免重算已经正确的部分。
    """
    heartbeat_task = asyncio.create_task(_heartbeat(pool, run_id, stop_event))
    completed = 0
    failed = 0
    source = ""
    try:
        async with pool.acquire() as conn:
            project = await conn.fetchrow("select name from projects where id::text=$1", project_id)
            all_rows = await conn.fetch(
                "select id, seq, level, title, content, words, status, source, "
                "coalesce(target_words,0) as target_words, coalesce(writing_focus,'') as writing_focus "
                "from project_chapters where project_id::text=$1 order by seq",
                project_id,
            )
            interp = await conn.fetchrow(
                "select dims, scores, coalesce(analysis_details,'[]'::jsonb) as analysis_details, "
                "coalesce(spec_digest,'') as spec_digest, "
                "coalesce(param_lock,'') as param_lock, "
                "coalesce(subject_meta,'{}'::jsonb) as subject_meta "
                "from project_interpret where project_id::text=$1", project_id
            )
            tender_row = await conn.fetchrow(
                "select f.text from project_files f left join project_interpret i on i.project_id=f.project_id "
                "where f.project_id::text=$1 order by "
                "(f.id::text=coalesce(i.score_meta->>'file_id','')) desc, f.created_at desc limit 1", project_id
            )
            # 招标原件字节（只给"按版式还原表格"用）。旧上传没有存字节 → None → 降级。
            # 这一步必须容错：拿不到原件绝不能让整次生成失败。
            tender_pdf = await tender_pdf_bytes(conn, project_id)
        # 与解读/大纲同一口径：先清洗每页重复的页眉页脚，避免过期页眉把别的标段
        # 的标的物（如「立柱、千斤」）当成本项目事实写进台账与逐章提示词。
        from zhibiao.core.source_clean import clean_tender_text
        tender_text = clean_tender_text(tender_row["text"]) if tender_row else ""
        # 结构化表格：整份 PDF **只解析一次**（83 页约 1~2 秒），逐章只做"定位 + 渲染"。
        # 每章都重开一遍 PDF 会把一次生成拖成十几遍解析。
        pdf_report = parse_pdf_tables(tender_pdf)
        from zhibiao.core.technical_evidence import extract_technical_evidence, render_technical_evidence, scope_numbered_lot_announcement, extract_material_evidence, render_grouped_source_clauses
        _pages = pdf_report.get("page_texts") or {}
        tender_text, _selected_lot = scope_numbered_lot_announcement(
            tender_text, str(_pages.get("1") or _pages.get(1) or ""),
            str(project["name"] or "") if project else "",
        )
        technical_evidence = extract_technical_evidence(pdf_report)
        material_evidence = extract_material_evidence(pdf_report)
        technical_context = render_technical_evidence(technical_evidence)
        grouped_clauses = render_grouped_source_clauses(pdf_report)
        format_span = format_chapter(tender_text) if pdf_report.get("ok") else None
        format_anchor = str((format_span or {}).get("title") or "").strip() or None
        format_sections_list = format_sections(tender_text) if pdf_report.get("ok") else []
        format_titles = [str(item.get("title") or "") for item in format_sections_list]
        format_labels = [str(item.get("label") or "") for item in format_sections_list]
        device_registry = extract_device_registry(tender_text)
        # 商务口径与技术参数同源：都从同一份清洗后的原文抽取，保证各章口径一致。
        commercial_terms = extract_commercial_terms(tender_text)
        commercial_context = render_commercial_context(commercial_terms)
        bill_context = render_bill_context(tender_text)
        requirement_inventory = build_requirement_inventory(tender_text)
        analysis_details = _json_value(interp.get("analysis_details"), []) if interp else []
        analysis_details = analysis_details if isinstance(analysis_details, list) else []
        if _selected_lot:
            # Older whole-document summaries can still contain the other lot.
            analysis_details = []
        if project is None or not all_rows:
            raise RuntimeError("项目或章节目录不存在")
        rows = [dict(row) for row in all_rows]
        targeted = {str(item) for item in (chapter_ids or []) if str(item or "").strip()}
        if targeted:
            rows = [row for row in rows if str(row.get("id") or "") in targeted]
            if not rows:
                raise RuntimeError("指定的章节不属于该项目，无法单项重新生成")
        pending = [row for row in rows if str(row.get("status") or "") != "已生成"]
        if not pending:
            async with pool.acquire() as conn:
                await conn.execute(
                    "update project_generation_runs set status='completed', finished_at=now(), "
                    "heartbeat_at=now(), message='所有章节均已生成' where id::text=$1",
                    run_id,
                )
                # 提前返回同样要复位项目状态：否则项目会永久显示「生成中」而没有任何任务。
                await conn.execute(
                    "update projects set status='待检查', updated_at=now() "
                    "where id::text=$1 and status='生成中'",
                    project_id,
                )
            return
        dims = _json_value(interp["dims"], []) if interp else []
        scores = _json_value(interp["scores"], []) if interp else []
        dims = dims if isinstance(dims, list) else []
        from zhibiao.core.tender_parse import refresh_tender_facts
        dims = refresh_tender_facts(dims, tender_text)
        scores = scores if isinstance(scores, list) else []
        spec_digest = str(interp["spec_digest"] or "") if interp is not None else ""
        if _selected_lot:
            spec_digest = ""
        param_lock = str(interp["param_lock"] or "") if interp is not None else ""
        if tender_text:
            # 从原文动态重建，旧项目无需重新跑大模型解读即可获得修正的设备归属。
            param_lock = build_param_lock(tender_text)
        if not param_lock:
            # 兜底：旧项目还没有参数锁时退回技术规格书要点，至少不比修复前更差
            param_lock = spec_digest
        _subject_meta = _json_value(interp["subject_meta"], {}) if interp is not None else {}
        # Recompute from the pinned original source so old projects also receive
        # equipment subtype guards; never reuse another project's category.
        if tender_text:
            _subject_meta = classify_subject(tender_text)
        industry_text = industry_guard_text(_subject_meta) if isinstance(_subject_meta, dict) else ""
        source_contract = render_primary_contract(tender_text)
        facts, ledger_text = build_project_ledger(
            str(project["name"] or ""), dims, scores,
            spec_digest=spec_digest, param_lock=param_lock, industry_text=industry_text,
            tender_text=tender_text,
        )
        # 事实缺失即拒绝生成（「请先完成全局事实设定，再生成正文」）。
        # 招标文件还没解读出任何可核查事实时，
        # 正文只能写空话，且模型在缺事实约束下极易编造专利号/业绩 —— 那是废标级风险。
        # 逃生开关：环境变量 ALLOW_GENERATE_WITHOUT_FACTS=1（运维级，不在界面上暴露）。
        _facts_ok, _facts_reason = facts_ready(str(project["name"] or ""), dims)
        if not _facts_ok and _env_float("ALLOW_GENERATE_WITHOUT_FACTS", 0.0) != 1.0:
            async with pool.acquire() as conn:
                await conn.execute(
                    "update project_generation_runs set status='failed', finished_at=now(), "
                    "heartbeat_at=now(), message=$2 where id::text=$1",
                    run_id, "生成已拒绝：" + _facts_reason,
                )
                await conn.execute(
                    "update projects set status='待生成', updated_at=now() where id::text=$1",
                    project_id,
                )
            logger.warning("[正文生成] 已拒绝：%s", _facts_reason)
            return
        planning_ledger = ledger_text
        if source_contract:
            planning_ledger += "\n\n" + source_contract
        if requirement_inventory.get("requirements"):
            plan_source = select_requirement_context(requirement_inventory,
                chapter="项目范围 交付成果 研发 供货清单 培训 验收 工期 售后 证据 知识产权", max_chars=10000)
            planning_ledger += "\n\n【跨章节源要求分工依据】\n" + plan_source["text"]
        planned = await _set_plan(
            pool, run_id, project_id, rows, planning_ledger, llm, configured, call_llm, chapter_hint
        )
        budgets = estimate_word_budgets(planned, target_words)
        # 调用间隔（对标 80 BMP_GEN_INTERVAL）：串行之外再留间隔，避免触发服务商限流。
        # 实测该模型单请求 45 tok/s、3 路并行合计 120 tok/s 且不限流，2 秒间隔纯属空等。
        _gen_interval = _env_float("GEN_INTERVAL", 0.5)
        # ── 多轮补齐 + 受限并发（用户要求：生成不了就跳过继续下一节，整本跑完再回头补失败的；
        #    除用户主动停止外不自动停下）────────────────────────────────────────────────
        # 每章原先要串行 3 次大模型调用（正文 + 字数校正 + 风险修订），48 章接近 1.6 小时，
        # 中途一断就整批作废。这里改成：单章独立、失败只记录不中断、多轮补齐、受限并发。
        passes = max(1, int(_env_float("GEN_PASSES", 3)))
        pass_interval = _env_float("GEN_PASS_INTERVAL", 15.0)
        concurrency = max(1, min(4, int(_env_float("GEN_CONCURRENCY", 3))))
        # 同时生成的项目各自分摊模型通道：单项目占满通道会让另一个项目看起来像被暂停。
        share = await parallel_share_for_project(pool, project_id)
        if share < concurrency:
            logger.info("[正文生成] 检测到其它项目在生成，本任务并发由 %d 降为 %d 以并行推进",
                        concurrency, share)
            concurrency = share
        # 单章最多要串行 5 次大模型调用（正文 / 截断续写 / 字数校正 / 风险修订 / 质量修订 ×2），
        # 每次各自有 300 秒超时；所以这里是「卡死安全网」而不是性能上限。
        # asyncio.to_thread 无法取消，超时后线程仍在跑，安全网必须给足，避免把慢但能成功的章节判失败。
        chapter_timeout = max(120.0, _env_float("GEN_CHAPTER_TIMEOUT", 1800.0))
        stats = {"completed": 0, "failed_tries": 0, "failed_ids": set(), "skipped": 0,
                 # 失败原因必须留到任务消息里：单章重新生成失败时，用户要能看到
                 # 「为什么没换掉」而不是一个不动的「生成中」。
                 "failed_reasons": {}}
        source_values: list[str] = []

        async def _run_update(**fields: Any) -> None:
            """更新任务进度（心跳一起刷新，避免被判为死任务）。"""
            if not fields:
                return
            assignments = ", ".join("%s=$%d" % (key, index + 2) for index, key in enumerate(fields))
            async with pool.acquire() as conn:
                await conn.execute(
                    "update project_generation_runs set " + assignments + ", heartbeat_at=now() "
                    "where id::text=$1",
                    run_id, *fields.values(),
                )

        async def _mark_failed(chapter: dict[str, Any], reason: str) -> None:
            stats["failed_reasons"][str(chapter.get("id"))] = (str(chapter.get("title") or ""),
                                                                str(reason))
            async with pool.acquire() as conn:
                await conn.execute(
                    "update project_chapters set status='生成失败', source=$2, updated_at=now() "
                    "where id=$1 and status <> '已生成'",
                    chapter["id"], ("生成失败：" + reason)[:500],
                )

        async def _generate_chapter(chapter: dict[str, Any], sem: asyncio.Semaphore,
                                   pass_no: int = 1) -> str:
            """生成一章。所有异常都记为该章失败，绝不影响其它章节。"""
            title_text = str(chapter.get("title") or "")
            text = ""
            error = ""
            chapter_source = ""
            # 本次生成最后一次模型调用是否被 max_tokens 切断（finish_reason=length）。
            # 收尾判据用它区分「模型明确被截断」与「自己写完了」：表格停在半途且有这个证据时，
            # 即使表格行本身列数齐全也算没写完（成稿 v9 的 6.2.5 / 7.1.6.1 / 9.2.10 就是这样）。
            _tail_cut = False
            async with sem:
                if stop_event.is_set():
                    return "skipped"
                if not _has_heading_title(title_text):
                    # 空标题章节不能交给模型：提示词里没有可写的题目，模型只会把相邻章节
                    # 已经写过的内容再铺一遍（成稿 v3 第 4 章「4.7」下的里程碑/人员分工/
                    # 交付物清单就是这么被重写出来的）。宁可跳过，也不产出重复正文。
                    logger.warning("[正文生成] 跳过空标题章节：seq=%s id=%s",
                                   chapter.get("seq"), chapter.get("id"))
                    await _run_update(
                        current_seq=chapter["seq"], current_title="",
                        message="第 %d 节标题为空，已跳过（不生成无标题章节）"
                                % int(chapter.get("seq") or 0),
                    )
                    return "skipped"
                # 只有真正开始生成时才更新「当前章节」，否则并发调度会把界面显示成最后一节
                await _run_update(
                    current_seq=chapter["seq"], current_title=title_text,
                    message=("正在生成第 %d 节「%s」" % (chapter["seq"], title_text)) if pass_no == 1
                    else ("第 %d 轮补齐第 %d 节「%s」" % (pass_no, chapter["seq"], title_text)),
                )
                if _gen_interval > 0:
                    await asyncio.sleep(_gen_interval)
                _numbers = _chapter_numbers(planned)
                _containers = container_chapter_numbers(_numbers.values())
                _chapter_number = str(_numbers.get(str(chapter["id"])) or str(chapter["seq"]))
                source_context = chapter_source_context(
                    requirement_inventory, analysis_details,
                    str(chapter.get("path") or title_text), str(chapter.get("writing_focus") or ""),
                )
                chapter_shared = _shared_ledger(ledger_text)
                # 商务硬约束（投标有效期/保证金/预算/工期/质保期/付款方式）此前完全没进
                # 提示词：技术标通篇不承诺投标有效期就是"未响应"，工期与质保期写错一个数
                # 就是格式9/10 规定的"负偏离作废标"。放在最前面，避免被长台账挤到末尾。
                if commercial_context:
                    chapter_shared += "\n\n" + commercial_context
                if source_contract:
                    chapter_shared += "\n\n" + source_contract
                # 清单基线（随机备件 24 项 / 千斤顶数量 2 根/组）：这些表在招标文件第 63～65 页，
                # param_lock 只收"主要技术参数"表，捡不到清单；不显式给出，模型只能凭印象写，
                # 实测写成"24 项列 12 项""数量写成按组配套"。放在设备参数表之前。
                if bill_context:
                    chapter_shared += "\n\n" + bill_context
                # 条文型条款（材质/工艺/涂装/破顶装置）：这些内容不在"主要技术参数"表里，
                # 台账与设备参数表都捡不到，实测于是写成"按招标人要求确定"，并漏掉
                # 破顶装置"巷道高度 3.0-3.5m / 收回 2.7m / 伸出 4.0m"。
                _clause = render_clause_context(
                    tender_text, str(chapter.get("path") or title_text),
                    str(chapter.get("writing_focus") or ""),
                )
                if _clause:
                    chapter_shared += "\n\n" + _clause
                # 「讲破顶就必须写全尺寸」这条硬要求的原文依据，原本只在目录路径/写作要点
                # 真的出现「破顶」时才随 _clause 注入。结构、工作原理、参数对照这类章节会
                # 写到端尾支架、也常带一句破顶，却没命中关键词 —— 模型手里没有
                # 3.0-3.5/2.7/4.0，质检却要求写全，于是必然失败（线上 6 章全栽在这一条）。
                # 处理原则：要求与依据必须同时到场。命中端尾/结构/参数类要点就主动补这段原文；
                # 是否真的套「写全」规则，由同一个判定传给质检（roof_in_scope）。
                _roof_hint = " ".join([title_text, str(chapter.get("path") or ""),
                                       str(chapter.get("writing_focus") or "")])
                _roof_in_scope = roof_completeness_scope(
                    title_text, str(chapter.get("path") or ""), str(chapter.get("writing_focus") or ""))
                if not _roof_in_scope and _ROOF_CLAUSE_FOCUS.search(_roof_hint):
                    _roof_clause = section_for(tender_text, "破顶", 1200)
                    if _roof_clause:
                        chapter_shared += ("\n\n【破顶装置原文条款（本章写到端尾支架/结构/参数时"
                                           "照抄数值，不得改写）】\n" + _roof_clause)
                # 分层设备参数表必须显式给出：真实标书的初撑力/工作阻力都在
                # 「××基本参数表」「立柱参数表」里，主参数清单抓不到，模型只能拼，
                # 实测会把初撑力当工作阻力写（10048 应为 12500），整章被判失败。
                _device_table = render_device_parameters(
                    device_registry, prefer=title_text + " " + str(chapter.get("writing_focus") or ""),
                )
                if _device_table:
                    chapter_shared += ("\n\n【设备参数表（招标原文，逐项照抄；"
                                       "整架值与单柱值不得混用，初撑力与工作阻力不得互换）】\n" + _device_table)
                if technical_context and re.search(r"技术|参数|性能|设备|供货|配置|结构", title_text + " " + str(chapter.get("writing_focus") or "")):
                    chapter_shared += "\n\n" + technical_context
                if grouped_clauses and re.search(r"技术|参数|性能|材质|材料|设备|结构|工艺|涂装", title_text + " " + str(chapter.get("writing_focus") or "")):
                    chapter_shared += "\n\n" + grouped_clauses
                if material_evidence and re.search(r"技术|参数|性能|材质|材料|设备|结构", title_text + " " + str(chapter.get("writing_focus") or "")):
                    chapter_shared += "\n\n【材质表逐行依据，部件与牌号不得互换】\n" + "\n".join("PDF第%s页｜%s｜%s" % (x['page'], x['owner'], x['value']) for x in material_evidence[:100])
                if source_context:
                    chapter_shared += "\n\n" + source_context
                # 法务文书（法定代表人身份证明 / 授权委托书）：招标文件给了格式就照抄，
                # 没给就用通用格式 + 红色待填，绝不交给模型编姓名与证件号。
                # 必须在材料占位之前判，否则会被当成「扫描件」整章替换成一句话。
                legal_text = legal_form_body(title_text, tender_text=tender_text, dims=dims)
                # 「信用/信誉查询证明」小节：内容就是招标文件点名的查询渠道清单，必须
                # 逐渠道一行 + 每行一个截图图框。交给模型写会漏渠道（实测 3 个渠道只写 2 个），
                # 图框数与说明还会互相矛盾，所以由 credit_proofs 按原文渠道清单直接出稿。
                credit_text = credit_proof_body(title_text, tender_text)
                # 其余「按招标文件格式填写」的章节（投标函 / 偏差表 / 报价表 / 资格审查 /
                # 承诺书 …）：形式评审看的就是这些表，必须照录招标原文，不能让模型改写成
                # 长篇说明。找不到对应格式原文（技术描述这类只给白纸的）才交给模型。
                # 有原件字节时走"结构化表格直出"（列数/合并/合计位置/注释归属来自版式），
                # 拿不到就退回文本启发式并在章节来源里标降级。
                form_structure = structure_from_report(
                    pdf_report, title_text, titles=format_titles, labels=format_labels,
                    chapter_anchor=format_anchor)
                format_text, form_meta = (None, {}) if (legal_text or credit_text) else \
                    format_chapter_text_ex(title_text, tender_text, facts, structure=form_structure)
                material_text = (legal_text or credit_text or format_text
                                 or material_insert_placeholder(title_text))
                # 照录招标格式的章节（投标函/身份证明/授权委托书/偏差表/报价表/资格审查/承诺书…）
                # 与「模型撰写的正文」是两类东西：它们的内容由招标人规定，平台只负责抄准。
                # 因此所有面向写作质量的门禁（断尾、偏差声明、篇幅、否决措辞、编号归一、
                # 数值归一）一律不套 —— 套上去只会把正确的格式判成失败。
                form_text = bool(legal_text or format_text)
                if legal_text:
                    text = legal_text
                    chapter_source = "格式文书（招标格式原文/通用格式，红色待填）"
                elif credit_text:
                    text = credit_text
                    chapter_source = "规则生成（信誉查询渠道逐条 + 每渠道一个截图图框）"
                elif format_text:
                    text = format_text
                    chapter_source = _format_source_label(form_meta)
                elif material_text:
                    text = material_text
                    chapter_source = "规则占位（材料原件需插入）"
                elif not configured:
                    text = safe_structured_draft(title_text, facts, scores)
                    chapter_source = "内置模板（结构草稿，需人工补充）"
                else:
                    length = budgets.get(chapter["id"], 0)
                    length_hint = (
                        "篇幅不限，按本节实际任务展开，不添加无依据内容"
                        if length <= 0 else
                        # 只给「约 N 字」时实测模型会写到目标的十几倍（850 字目标写出 1.1 万字）。
                        # 明确给出上限口径，把篇幅从「建议」变成「上限」。
                        "约 %d 字，硬性上限 %d 字（表格式参数清单另计，但不得重复铺陈、注水或复述其它章节）"
                        % (length, int(length * WORD_LIMIT_RATIO))
                    )
                    prompt = _safe_format(writer_prompt, dict(
                        title=title_text,
                        seq=chapter["seq"],
                        number=_numbers.get(str(chapter["id"]), str(chapter["seq"])),
                        total=len(planned),
                        path=chapter.get("path") or title_text,
                        focus=chapter.get("writing_focus") or "按本节标题和全书事实确定内容范围",
                        scope=_chapter_scope(chapter),
                        # 台账在 `shared` 消息里已经整份带上（且跨章完全相同，能命中前缀缓存）。
                        # 这里如果再贴一遍，每章每次调用就多付约 1.4 万 token 的输入，
                        # 而且这一遍在缓存断点之后，是最不值钱的位置。改成指针，内容不丢。
                        ledger=("（全书统一事实与承诺口径见前一条「全书统一事实与承诺口径」消息："
                                "其中【唯一参数口径】【技术规格书要点】等必须逐字遵守，"
                                "数值、设备归属、数量、牌号与期限不得改写）"),
                        scores=_score_context(title_text, str(chapter.get("writing_focus") or ""), scores)[:5000],
                        length=length_hint,
                        others="、".join(
                            "%s %s" % (_numbers.get(str(item["id"]), ""), str(item.get("title") or ""))
                            for item in planned if item["id"] != chapter["id"]
                        )[:4000] or "无",
                        issues="\n".join("- %s" % item for item in issues[:10]) if issues else "（无）",
                    ))
                    try:
                        from app.kb_api import retrieve_context

                        rag_query = " ".join((
                            title_text,
                            str(chapter.get("writing_focus") or ""),
                            _score_context(title_text, str(chapter.get("writing_focus") or ""), scores)[:1800],
                        ))
                        rag_context = await retrieve_context(pool, owner, rag_query, top_k=6)
                        if rag_context:
                            prompt += "\n\n" + rag_context
                    except Exception as exc:
                        # 知识库不可用绝不能拖垮本来可以正常生成的章节
                        logger.warning("[正文生成] 知识库检索失败，继续不带素材生成：%s", exc)
                    try:
                        _meta_llm = _resolve_meta_llm(call_llm)
                        # 初稿/续写/扩写都按本节篇幅上限收紧 max_tokens：
                        # 只收紧"往长里写"的调用；质量修订与收敛仍用原配置，
                        # 否则会把需要整章重写的修订稿截断成半句。
                        _llm_write = _chapter_llm(llm, int(budgets.get(chapter["id"], 0) or 0))
                        if _meta_llm is not None:
                            ok, output, _meta = await asyncio.to_thread(
                                _meta_llm, _llm_write, prompt, int(llm.get("timeout", 300)), writer_system,
                                chapter_shared,
                            )
                        else:
                            ok, output = await asyncio.to_thread(
                                call_llm, _llm_write, prompt, int(llm.get("timeout", 300)), writer_system,
                                chapter_shared,
                            )
                            _meta = {}
                        if ok and str(output or "").strip():
                            text = str(output).strip()
                            chapter_source = ("大模型 %s" % (llm.get("model") or "")).strip()
                            # 截断续写：finish_reason=length，或「又短又没写完」时带上下文接着写
                            if _meta_llm is not None:
                                _cont_state: dict[str, Any] = {}
                                text, _cont_rounds = await _continue_until_complete(
                                    _meta_llm, _llm_write, prompt, writer_system, chapter_shared, text,
                                    str(_meta.get("finish_reason") or ""),
                                    int(budgets.get(chapter["id"], 0) or 0),
                                    state=_cont_state,
                                )
                                # 以最后一次调用的 finish_reason 为准（续写可能补上了，也可能又被切了）
                                _tail_cut = str(_cont_state.get("finish_reason") or "") == "length"
                                if _cont_rounds:
                                    chapter_source = "%s（截断续写×%d）" % (chapter_source, _cont_rounds)
                        else:
                            error = str(output or "模型未返回正文")
                    except Exception as exc:
                        error = "模型调用异常：%s" % exc

                # 字数事后校正（1 轮、不下调目标）
                _target_words = int(budgets.get(chapter["id"], 0) or 0)
                if configured and text and _target_words > 0 and not material_text and not stop_event.is_set():
                    _current_words = len(re.sub(r"\s", "", text))
                    if _current_words < _target_words * 0.75:
                        try:
                            _ok2, _expanded = await asyncio.to_thread(
                                call_llm, _chapter_llm(llm, _target_words),
                                _expand_prompt(title_text, text, _target_words, _current_words)
                                + "\n\n参数、设备归属、数量、标准与期限以全书统一台账为准；台账已有的数据不得改成待填。",
                                int(llm.get("timeout", 300)), writer_system, chapter_shared,
                            )
                            _new_words = len(re.sub(r"\s", "", str(_expanded or "")))
                            if _ok2 and _new_words > _current_words:
                                text = str(_expanded).strip()
                                chapter_source = (chapter_source + "（字数校正）").strip()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("[字数校正] 章节 %s 扩写失败：%s", title_text, exc)
                # 投标正文只保留肯定响应；先尝试一次定向修订，仍有风险术语则不落库
                forbidden_terms = [] if form_text else find_forbidden_bid_terms(text)
                if forbidden_terms and configured and not material_text and not stop_event.is_set():
                    try:
                        ok, revised = await asyncio.to_thread(
                            call_llm, llm, repair_prompt(title_text, text, forbidden_terms),
                            int(llm.get("timeout", 300)), writer_system, chapter_shared,
                        )
                        if ok and str(revised or "").strip():
                            text = str(revised).strip()
                            chapter_source = "%s（正文风险修订）" % chapter_source
                    except Exception as exc:
                        logger.warning("[正文风险拦截] 章节修订失败 %s: %s", title_text, exc)
                    forbidden_terms = [] if form_text else find_forbidden_bid_terms(text)
                if forbidden_terms:
                    error = "正文包含禁止的否决/无效投标风险表述，修订后仍未通过内容拦截"
                    text = ""

                # 必须**先归一编号再质检**：否则质检新增的编号规则会把每一章都判成
                # 「内嵌小标题编号与目录编号冲突」，两轮修订后整章被判失败。
                if form_text:
                    _renumbered = False
                else:
                    text, _renumbered = _renumber_inline(text, _chapter_number, _containers)
                if _renumbered:
                    chapter_source = (chapter_source + "（内嵌编号归一）").strip()
                # 招标人侧信息不该由投标人待填：模型万一仍写了 {{待填:招标人…}}，
                # 这里机械改写成「由招标人指定」，不必再花一轮修订，也不让它进存量正文。
                text, _neutralized = neutralize_owner_placeholders(text)
                if _neutralized:
                    chapter_source = (chapter_source + "（招标人待填归一）").strip()
                # 插图占位框归一：模型可能写 `{{图片占位:系统架构图}}` 或旧写法
                # `【图片占位】…`，统一改成 image_slots 的语法并把短图名补成
                # 「插什么 + 要不要盖章 + 要不要截全」的具体要求（导出与前端同一份口径）。
                text, _slot_changes = normalize_image_placeholders(text)
                if _slot_changes:
                    chapter_source = (chapter_source + "（插图占位框）").strip()
                # 单位笔误（湿度写成 ℃）与失效的章节交叉引用（按 10.1.1.3 → 10.1.3）：
                # 这类错误模型自己改不掉（它看不到目录），也不值得判整章失败，机械归一即可。
                if form_text:
                    _text_notes = []
                else:
                    text, _text_notes = normalize_generated_text(text, set(_numbers.values()))
                # 正文卫生：清掉误抄进来的写作指令、收掉「一 年」这类量词空格、
                # 删掉与后文重复的章首导言。都是机械可判的事，改不改得对不靠模型。
                text, _hygiene_notes = hygienic_text(text)
                # 招标文件明确「不接受代理商投标」时，正文不能出现「投标人为代理经销商时，
                # 同时附制造商相应资质材料」这类把自己写成代理商的假设句 —— 那是自己声明
                # 自己不合格。条件全部来自招标原文：允许代理投标的项目原样保留。
                # 照录招标格式的章节（form_text）是招标人原文，不改。
                if form_text:
                    _agency_notes = []
                else:
                    text, _agency_notes = suppress_agency_assumptions(text, tender_text)
                # 同一张供货明细表里「永磁…滚筒」与「传动滚筒」并列时，表后补一行
                # 「不重复计价」备注：只加给人看的提示，表格行与合计口径一个字不动。
                text, _pricing_note = add_drum_pricing_note(text)
                _text_notes = list(_text_notes) + _hygiene_notes + list(_agency_notes)
                if _pricing_note:
                    _text_notes.append("表后补不重复计价提示")
                if _text_notes:
                    chapter_source = (chapter_source + "（" + "、".join(_text_notes) + "）").strip()
                if form_text:
                    quality_issues = validate_format_chapter(text, title_text)
                else:
                    quality_issues = _validate_chapter(
                        text, title_text, device_registry, tender_text,
                        allow_material=bool(material_text), chapter_number=_chapter_number,
                        commercial=commercial_terms, roof_in_scope=_roof_in_scope, technical_evidence=technical_evidence, material_evidence=material_evidence,
                    )
                # 篇幅下限：实测出现过 413 字的「本章各部分分工如下」式导语被当成整章落库
                # （终稿 413 字，目标上千字）。续写判据里「结尾是句号就算写完」是根因，
                # 这里再加一道兜底：明显短于预算就当没写完，让质量修订把正文展开。
                _budget_words = int(budgets.get(chapter["id"], 0) or 0)
                # 材料占位与按格式填写的法务文书（身份证明/授权委托书）本来就不按预算铺陈：
                # 拿「篇幅偏短」去卡它们，会把这类整章判失败（修好内容反而被字数门禁拦掉）。
                if text and _budget_words > 0 and not material_text:
                    _actual_words = len(re.sub(r"\s", "", text))
                    if _actual_words < int(_budget_words * 0.4):
                        quality_issues.append(
                            "正文明显偏短：仅 %d 字，本章预算 %d 字，须按小节完整展开（不得只写导语或章节目录说明）"
                            % (_actual_words, _budget_words)
                        )
                # 章末断尾（`章末半句断尾…` / `正文末句未完成…`）是结构未完成，不是内容错误：
                # 它由落库前的收尾续写专门处理（见下面的 _finish_chapter_tail），绝不能在这里
                # 清零整章 —— 线上 seq=14 的 800 字正文只因为末句缺句号就被判「正文质量未通过」，
                # 用户拿到的是 content=0 的空白章。先按内容错误修，修不掉的再判。
                _blocking = _blocking_quality_issues(quality_issues)
                if (_blocking and not form_text and configured
                        and not material_text and not stop_event.is_set()):
                    for _quality_try in range(2):
                        try:
                            _okq, _fixed = await asyncio.to_thread(
                                call_llm, llm, quality_repair_prompt(title_text, text, quality_issues),
                                int(llm.get("timeout", 300)), writer_system, chapter_shared,
                            )
                            if _okq and str(_fixed or "").strip():
                                candidate = str(_fixed).strip()
                                # 最小修订不能用一小段摘要替换整章。
                                if len(candidate) >= min(200, len(text)) and (not text or len(candidate) >= len(text) * 0.60):
                                    candidate, _rn = _renumber_inline(candidate, _chapter_number, _containers)
                                    text = candidate
                                    chapter_source = (chapter_source + "（来源质量修订）").strip()
                        except Exception as exc:
                            logger.warning("[正文质量] 修订失败 %s: %s", title_text, exc)
                        quality_issues = _validate_chapter(
                            text, title_text, device_registry, tender_text,
                            allow_material=bool(material_text), chapter_number=_chapter_number,
                            commercial=commercial_terms, roof_in_scope=_roof_in_scope, technical_evidence=technical_evidence, material_evidence=material_evidence,
                        )
                        quality_issues += ["修订后出现禁止风险表述：" + x for x in find_forbidden_bid_terms(text)]
                        _blocking = _blocking_quality_issues(quality_issues)
                        if not _blocking:
                            error = ""
                            break
                if _blocking:
                    error = "正文质量未通过：" + "；".join(_blocking[:5])
                    # 失败必须留下证据。以前这里把草稿整段丢掉、也不打日志，
                    # 事后无法判断到底是「模型写错了参数」还是「校验器判错了」，
                    # 只能看到前端一句"生成失败"（用户实际反馈的问题）。
                    logger.warning(
                        "[正文质量] 章节 %s 未通过（%d 项）：%s | 草稿开头：%s",
                        title_text, len(_blocking), "；".join(_blocking[:3]),
                        re.sub(r"\s+", " ", text)[:600],
                    )
                    text = ""
                elif quality_issues:
                    # 只剩章末断尾：保留正文，交给落库前的收尾续写。续得上就补全；
                    # 续不上则原样保留 + 在 chapter_source 记事实说明（见 _finish_chapter_tail）。
                    logger.info("[正文质量] 章节 %s 仅章末断尾（%s），交由收尾续写处理",
                                title_text, "；".join(quality_issues[:3]))
                if error and not text:
                    if "正文质量未通过" not in error:
                        logger.warning("[正文生成] 章节 %s 生成失败：%s", title_text, error)
                    await _mark_failed(chapter, error)
                    source_values.append("大模型生成失败")
                    return "failed"

                # 篇幅收敛（落库前兜底）：质量/风险修订都会重发整章，实测会把篇幅越修越长
                # （850 字目标写出 1.1 万字）。只在超出上限时改写，且只有更短、仍能通过
                # 质检与合规校验的版本才会被采用——绝不为了字数丢掉参数或承诺。
                _limit_words = int(budgets.get(chapter["id"], 0) or 0)
                if (configured and text and _limit_words > 0 and not material_text
                        and not stop_event.is_set()):
                    _ceiling = _trim_ceiling(_limit_words)
                    for _trim_round in range(WORD_TRIM_ROUNDS):
                        _current_words = len(re.sub(r"\s", "", text))
                        if _ceiling <= 0 or _current_words <= _ceiling:
                            break
                        try:
                            _okt, _condensed = await asyncio.to_thread(
                                call_llm, llm,
                                _condense_prompt(title_text, text, _limit_words, _current_words),
                                int(llm.get("timeout", 300)), writer_system, chapter_shared,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("[篇幅收敛] 章节 %s 收敛失败：%s", title_text, exc)
                            break
                        if not _okt:
                            # 失败时返回的往往是错误说明文本，绝不能当正文收下
                            logger.warning("[篇幅收敛] 章节 %s 收敛调用未成功，保留原稿", title_text)
                            break
                        _candidate = str(_condensed or "").strip()
                        _candidate, _rn = _renumber_inline(_candidate, _chapter_number, _containers)
                        _accepted, _why = _accept_condensed(
                            _candidate, text,
                            lambda body: _validate_chapter(
                                body, title_text, device_registry, tender_text,
                                allow_material=bool(material_text), chapter_number=_chapter_number, commercial=commercial_terms,
                                technical_evidence=technical_evidence, material_evidence=material_evidence,
                            ) + ["收敛后出现禁止风险表述：" + x
                                 for x in find_forbidden_bid_terms(body)],
                            _limit_words,
                        )
                        if not _accepted:
                            logger.warning("[篇幅收敛] 章节 %s 收敛稿被拒（%s），保留原稿",
                                           title_text, _why)
                            break
                        text = _candidate
                        chapter_source = (chapter_source + "（篇幅收敛）").strip()

                # 收尾处理（落库前最后一道）：续写/收敛/修订都可能把正文截在半个字、半个表格行，
                # 或者「表格只写了一半就收章、直接跳到下一节」。实测成稿 v9 同一份稿子里 6 处：
                # 导言只写「3. 端尾」、6.2.5 表只到序号 11、7.1.6.1 表只到序号 7、
                # 9.1.7 第 2 条是半句话、9.2.10 表只到序号 4、全文末表 10.2.7 只有两行。
                # 旧判据只查「末行有没有以 | 结尾 / 是不是半个字」，这四类全漏判（见 tail_repair）。
                # 这里先**续写**（从断点前最后一句/最后一行表格接着写）；续不出来时正文一字不改，
                # 只把「结尾未完、原文末句是什么」记进 chapter_source 并记 incomplete ——
                # 绝不用「综上所述」式通用收尾句伪造完整，也绝不因为断尾丢掉整章。
                # 照录招标格式/材料占位的章节（form_text / material_text）是招标人原文，一个字不动。
                # 这里不再额外用 stop_event 挡一道：_finish_chapter_tail 自己会在每轮开头检查停止，
                # 被停止时不会调模型，但仍会把「结尾未完」的事实记进 chapter_source（正文照旧落库）。
                if configured and text and not form_text and not material_text:
                    # 原文同表条目只对「章末停在表格行上」的章节有意义（其余章节连原文都不用扫）：
                    # 拿到它才能判「表内最后序号 < 原文同表最大序号」，也才能把缺的条目名交给模型
                    # 照着写。取不到就只退回纯结构判据（tail_repair 已按此设计），绝不猜。
                    _tail_last = (text.rstrip().splitlines() or [""])[-1].strip()
                    _source_items = (_source_table_items(tender_text, title_text)
                                     if _tail_last.startswith("|") else [])
                    text, _tail_notes, _tail_incomplete, _tail_note = await _finish_chapter_tail(
                        text, title=title_text, call_llm=call_llm, llm=llm,
                        writer_system=writer_system, chapter_shared=chapter_shared,
                        source_items=_source_items, truncated=bool(_tail_cut),
                        stop_event=stop_event,
                    )
                    if _tail_notes:
                        chapter_source = (chapter_source + "（" + "、".join(
                            dict.fromkeys(_tail_notes)) + "）").strip()
                    if _tail_incomplete:
                        # 状态列必须保持「已生成」：导出/前端都按它取正文（status <> '已生成'
                        # 会被当成没写完而整章跳过），把整章正文连同人工补齐说明一起丢掉更糟。
                        # 未完成的事实记在来源里，导出/检查都能看到「incomplete」。
                        chapter_source = (chapter_source + "（收尾未完成：incomplete，需人工补齐）").strip()
                        if _tail_note:
                            # 再记一条只说事实的中性说明（原文末句是什么、建议人工补齐），
                            # 模型写出的正文一个字不改、也不补任何「收尾句」。
                            chapter_source = (chapter_source + "（" + _tail_note + "）").strip()

                # 拼接残片（落库前）：模型把表格行写成两行、或把一行的后半段甩到表外
                # （实测 10.2.10 第 11~15 条：`| 11 | 机尾 | 半句原句 |` 换行后再
                # `| {{待填:…}} | … |`，表外还多一行 `11 | 机尾`），以及段末半句
                # （12.2.7「…接受招标人及矿方的安全、质量」后面没了）。这两类断点在
                # 段中/表内，章末断尾判定看不到，必须在落库前按结构证据拼回去。
                text, _join_notes = _repair_join_residue(text)
                if not form_text and not material_text:
                    from zhibiao.core.bid_integrity import repair_generated_text
                    text, _integrity_notes = repair_generated_text(
                        text, title_text, project_name=str(project["name"] or ""))
                    for _note in _integrity_notes:
                        logger.info("[事实依据修复] 章节 %s %s", title_text, _note)
                for _note in _join_notes[:4]:
                    logger.info("[拼接修复] 章节 %s %s", title_text, _note)
                if not form_text and not material_text:
                    from zhibiao.core.technical_evidence import technical_evidence_issues, quantity_breakdown_issues, material_evidence_issues, process_scope_issues
                    _final_technical_issues = technical_evidence_issues(text, technical_evidence) + quantity_breakdown_issues(text) + material_evidence_issues(text, material_evidence) + process_scope_issues(text, tender_text)
                    if _final_technical_issues:
                        await _mark_failed(chapter, "原表参数核对未通过：" + "；".join(_final_technical_issues[:5]))
                        source_values.append("原表参数核对未通过")
                        return "failed"
                words = len(re.sub(r"\s", "", text))
                # 空章才是失败：正文为空或几乎为空（< MIN_CHAPTER_BODY_CHARS）才判「生成失败」。
                # 到了这里正文已经是「模型写出的实质内容 ± 续写」，断尾不再清零整章 ——
                # 续不上就原样落库，结尾未完的事实记在 chapter_source 里（见收尾处理）。
                # 照录招标格式（form_text）与材料占位（material_text）本来就不是模型正文，豁免。
                if not form_text and not material_text and words < MIN_CHAPTER_BODY_CHARS:
                    error = ("正文为空或几乎为空（%d 字，少于 %d 字），无法落库"
                             % (words, MIN_CHAPTER_BODY_CHARS))
                    logger.warning("[正文生成] 章节 %s 生成失败：%s", title_text, error)
                    text = ""
                    await _mark_failed(chapter, error)
                    source_values.append("大模型生成失败")
                    return "failed"
                async with pool.acquire() as conn:
                    result = await conn.execute(
                        "update project_chapters set content=$2, words=$3, status='已生成', source=$4, "
                        "updated_at=now() where id=$1 and status <> '已生成'",
                        chapter["id"], text, words, chapter_source,
                    )
                if str(result).endswith(" 0"):
                    return "skipped"
                source_values.append(chapter_source)
                async with pool.acquire() as conn:
                    agg = await conn.fetchrow(
                        "select count(*) as c, coalesce(sum(words),0) as w from project_chapters "
                        "where project_id::text=$1",
                        project_id,
                    )
                    await conn.execute(
                        "update projects set chapter_count=$2, word_count=$3, updated_at=now() "
                        "where id::text=$1",
                        project_id, agg["c"], agg["w"],
                    )
                return "done"

        async def _one(chapter: dict[str, Any], sem: asyncio.Semaphore, pass_no: int) -> str:
            title_text = str(chapter.get("title") or "")
            try:
                outcome = await asyncio.wait_for(
                    _generate_chapter(chapter, sem, pass_no), timeout=chapter_timeout
                )
            except asyncio.TimeoutError:
                # 单章再慢也不拖死整个任务：记为失败，交给后续轮次补齐
                outcome = "failed"
                stats["failed_tries"] += 1
                await _mark_failed(chapter, "单章处理超过 %d 秒仍未完成，已跳过" % int(chapter_timeout))
                logger.warning("[正文生成] 章节 %s 处理超时（%ds），已跳过并留待后续轮次",
                               title_text, int(chapter_timeout))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单章异常绝不能终止整轮
                outcome = "failed"
                stats["failed_tries"] += 1
                await _mark_failed(chapter, "生成异常：%s" % exc)
                logger.warning("[正文生成] 章节 %s 生成异常，已跳过：%s", title_text, exc)
            else:
                if outcome == "failed":
                    stats["failed_tries"] += 1
                    stats["failed_ids"].add(str(chapter.get("id")))
            if outcome == "done":
                stats["completed"] += 1
            elif outcome == "skipped":
                stats["skipped"] += 1
            await _run_update(
                completed=stats["completed"], failed=len(stats["failed_ids"]),
                source=(source_values[-1] if source_values else ""),
                message=("已保存第 %d 节「%s」" % (chapter["seq"], title_text)) if outcome == "done"
                else ("第 %d 节「%s」本轮未成功，已跳过，稍后补齐" % (chapter["seq"], title_text)),
            )
            return outcome

        passes_used = 0
        for pass_no in range(1, passes + 1):
            if stop_event.is_set():
                break
            async with pool.acquire() as conn:
                run_state = await conn.fetchval(
                    "select status from project_generation_runs where id::text=$1", run_id
                )
                if run_state == "stopping":
                    stop_event.set()
                    break
                pending_rows = await conn.fetch(
                    "select id from project_chapters where project_id::text=$1 and status <> '已生成' "
                    "order by seq",
                    project_id,
                )
            pending_ids = {str(item["id"]) for item in pending_rows}
            targets = [item for item in planned if str(item["id"]) in pending_ids]
            if not targets:
                break
            passes_used = pass_no
            if pass_no > 1:
                await _run_update(
                    message="第 %d 轮补齐：%d 节此前未成功，正在回头重试" % (pass_no, len(targets))
                )
                if pass_interval > 0 and not stop_event.is_set():
                    await asyncio.sleep(pass_interval)
            else:
                await _run_update(message="开始生成 %d 节正文" % len(targets))
            # 每轮都重新分摊：其它项目可能在本任务运行期间才开始或结束。
            share = await parallel_share_for_project(pool, project_id)
            round_concurrency = max(1, min(concurrency, share))
            if round_concurrency < concurrency:
                await _run_update(
                    message=("开始生成 %d 节正文（与其它项目并行，本任务并发 %d）"
                             % (len(targets), round_concurrency))
                )
            sem = asyncio.Semaphore(round_concurrency)
            before = stats["completed"]
            # 工作队列而不是「分批等最慢的一章」：原来把章节切成 concurrency 一批、批内并行、
            # 批间串行，一章超时就会让同批其它通道空转（实测 48 章里有 3 章卡到 1800 秒，
            # 整轮被拖慢近半小时）。这里改成固定数量的 worker 反复取下一章，谁先写完谁接活。
            # 单章超时仍然只包住"取到活的这一章"，排队时间不计入（否则会集体误判超时）。
            _queue: asyncio.Queue = asyncio.Queue()
            for chapter in targets:
                _queue.put_nowait(chapter)

            async def _worker() -> None:
                while not stop_event.is_set():
                    try:
                        chapter = _queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        await _one(chapter, sem, pass_no)
                    except Exception as exc:  # noqa: BLE001 - worker 不能因单章异常退出
                        logger.warning("[正文生成] worker 处理异常：%s", exc)

            await asyncio.gather(*[_worker() for _ in range(round_concurrency)], return_exceptions=True)
            if stats["completed"] == before and not stop_event.is_set():
                # 整轮毫无进展（例如额度耗尽/服务不可用），再重试只是白烧额度
                logger.warning("[正文生成] 第 %d 轮无任何进展，停止补轮", pass_no)
                break

        async with pool.acquire() as conn:
            content_rows = await conn.fetch(
                "select id, seq, level, title, content, coalesce(source,'') as source "
                "from project_chapters "
                "where project_id::text=$1 and status='已生成' order by seq",
                project_id,
            )
            remaining = await conn.fetchval(
                "select count(*) from project_chapters where project_id::text=$1 and status <> '已生成'",
                project_id,
            )
        completed = stats["completed"]
        failed = int(remaining or 0)
        consistency: list[dict[str, Any]] = []
        _chapter_rows = [dict(row) for row in content_rows]
        # ── 整册后处理：半截「技术参数逐条响应」对照表 + 代理商身份表述 ──────────────
        # 这两件事必须看整册：对照表要跟后文那张完整表比，代理口径要跟招标原文比。
        # 单章生成时已各做一次，这里兜底续跑/单章重跑/旧任务留下的正文 —— 只在真有命中时
        # 才写库，绝不为了「跑一遍」而改动没问题的章节。
        try:
            _postfix_notes = await _document_postfix(pool, _chapter_rows, tender_text)
            if _postfix_notes:
                logger.info("[文档后处理] %s", "；".join(_postfix_notes[:8]))
        except Exception as exc:  # noqa: BLE001 - 后处理失败绝不能把已生成正文判成失败
            logger.warning("[文档后处理] 未完成，已跳过：%s", exc)
        try:
            consistency = scan_cross_chapter_conflicts(_chapter_rows)
        except Exception as exc:
            logger.warning("[正文一致性] 跨章节核对失败，已忽略: %s", exc)
        # 全文级核对：边界条款是否被响应、单台数量与总量口径是否矛盾（只告警，不改写）。
        try:
            _scope = scan_document_scope(_chapter_rows, tender_text)
            if _scope:
                consistency = list(consistency or []) + _scope
        except Exception as exc:  # noqa: BLE001
            logger.warning("[正文一致性] 供货边界/数量口径核对失败，已忽略: %s", exc)
        # 产出侧事实台账用于补充跨章节告警（含专利号），不凭未绑定对象的数值
        # 自动改写正文；实际修订由上面的来源质量门禁负责。
        repairs: dict[str, Any] = {}
        try:
            if _chapter_rows and not stop_event.is_set():
                _repair_rows = [
                    {"id": r.get("id"), "seq": r.get("seq"), "title": r.get("title"),
                     "content": r.get("content"), "status": "已生成"}
                    for r in _chapter_rows
                ]
                # 用产出侧台账产生更完整的告警（额外覆盖专利号等身份性数据）
                _fact_ledger = extract_chapter_facts(_chapter_rows)
                _rich = conflicts_from_facts(_fact_ledger)
                if _rich:
                    consistency = _rich
                # 旧全局标签未绑定设备对象/起算条件，只用于告警，不再直接替换任何章节数值。
                # 所有实际修订已在上面的来源质量门禁中携带完整原文、分设备台账完成并验收。
                repairs = {"repairs": [], "chapters": []}

        except Exception as exc:  # noqa: BLE001 - 修正失败绝不影响已生成正文
            logger.warning("[正文一致性] 自动修正失败，已忽略: %s", exc)
        # ── 生成后自检：用**同一套检查引擎**核对自己刚写出来的正文 ───────────────
        # 生成侧的质量门禁只管「这一章合不合格」，检查引擎管「整册是否满足招标要求」，
        # 两套口径不对齐时就会出现用户实测的现象：正文全部生成成功，回头一检查，
        # 自己写的标书还有几项「必须修改」。这里在收尾时真跑一遍检查，
        # 对「必须修改」项自动修复（最多两轮），修不好就转「待人工确认」并写明轮次。
        post: dict[str, Any] = {}
        if not remaining and not stop_event.is_set() and _env_float("GEN_POSTCHECK", 1.0) != 0.0:
            try:
                from zhibiao.core.postcheck import run_after_generation

                post = await run_after_generation(
                    pool, project_id, project_name=str(project["name"] or ""),
                    chapters=_chapter_rows, source_text=tender_text, dims=dims, scores=scores,
                    llm=llm, configured=configured, call_llm_fn=call_llm, owner=owner,
                )
            except Exception as exc:  # noqa: BLE001 - 自检失败不能把已完成的生成判成失败
                logger.warning("[生成后自检] 未完成，已跳过：%s", exc)
                post = {}
        stopped = stop_event.is_set()
        if stopped:
            final_status, message = "stopped", "已按请求停止；已完成章节均已保存，可继续生成剩余章节"
        elif remaining:
            # 多轮补齐后仍有章节没成功：不叫「暂停」，明确说明可再次续跑（用户未主动停止时也已在服务端继续）
            final_status = "completed_with_errors"
            message = ("共 %d 轮补齐；%d 节仍未成功（单章失败不影响其它章节），"
                       "可再次点「继续生成」重试" % (max(1, passes_used), remaining))
        else:
            final_status = "completed"
            message = "全部章节已生成（共 %d 轮）" % max(1, passes_used)
        if targeted:
            # 单项重新生成必须说清「换没换掉」：成功给字数，失败给具体原因。
            if remaining and stats["failed_reasons"]:
                _title, _reason = next(iter(stats["failed_reasons"].values()))
                message += "；「%s」未能重新生成：%s" % (_title, re.sub(r"\s+", " ", _reason)[:180])
            elif not remaining:
                _row = next((r for r in _chapter_rows if str(r.get("id")) in targeted), None)
                if _row is not None:
                    message = ("「%s」已重新生成，正文已替换（%d 字）"
                               % (str(_row.get("title") or ""),
                                  len(re.sub(r"\s", "", str(_row.get("content") or "")))))
        if post.get("ran"):
            if post.get("downgraded"):
                message += ("；自检：%d 项引擎已尝试 %d 轮自动修复未能通过，已转「待人工确认」"
                            "（见检查报告）" % (len(post["downgraded"]), max(1, int(post.get("rounds") or 0))))
            else:
                message += ("；自检通过：检查引擎未发现「必须修改」项")
                if post.get("repaired"):
                    message += "（自动修复 %d 项）" % len(post["repaired"])
        if repairs.get("repairs"):
            message = message + "；" + repair_summary(repairs)
        async with pool.acquire() as conn:
            await conn.execute(
                "update project_generation_runs set status=$2, completed=$3, failed=$4, source=$5, "
                "consistency=$6::jsonb, message=$7, current_seq=null, current_title=null, "
                "finished_at=now(), heartbeat_at=now() where id::text=$1",
                run_id, final_status, completed, failed, source,
                json.dumps(consistency, ensure_ascii=False), message,
            )
            await conn.execute(
                "update projects set status=$2, updated_at=now() where id::text=$1",
                project_id, "编制中" if remaining else "待检查",
            )
    except asyncio.CancelledError:
        logger.warning("[正文生成] 任务被取消 run_id=%s；已落库章节保留", run_id)
        async with pool.acquire() as conn:
            await conn.execute(
                "update project_generation_runs set status='interrupted', "
                "message='服务进程停止，已保存章节保留', finished_at=now(), heartbeat_at=now() "
                "where id::text=$1 and status in ('queued','planning','running','stopping')",
                run_id,
            )
        raise
    except Exception as exc:
        logger.exception("[正文生成] 后台任务失败 run_id=%s", run_id)
        async with pool.acquire() as conn:
            await conn.execute(
                "update project_generation_runs set status='failed', message=$2, finished_at=now(), "
                "heartbeat_at=now() where id::text=$1",
                run_id, ("任务异常：" + str(exc))[:500],
            )
            # 失败也要复位项目状态：否则项目永久显示「生成中」，用户以为还在跑。
            _remaining = await conn.fetchval(
                "select count(*) from project_chapters where project_id::text=$1 and status <> '已生成'",
                project_id,
            )
            await conn.execute(
                "update projects set status=$2, updated_at=now() where id::text=$1 and status='生成中'",
                project_id, "编制中" if int(_remaining or 0) else "待检查",
            )
    finally:
        stop_event.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        _TASKS.pop(run_id, None)
        _STOP_EVENTS.pop(run_id, None)


def task_status_is_active(run_id: str) -> bool:
    task = get_task(run_id)
    return task is not None and not task.done()


# ══════════════════ 对标审计新增：分层 / 间隔 / 字数校正 / 一致性修正 ══════════════════

def _env_float(name: str, default: float) -> float:
    """读环境变量里的浮点配置（对标 80 端口 BMP_GEN_INTERVAL）。"""
    import os

    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def _model_channel_limit() -> int:
    """进程级模型通道总量（与 pipeline_api._LLM_GATE 同一环境变量口径）。"""
    import os

    try:
        return max(1, int(os.environ.get("LLM_CONCURRENCY", "4") or 4))
    except (TypeError, ValueError):
        return 4


async def parallel_share_for_project(pool: Any, project_id: str) -> int:
    """多项目同时生成时，按「当前在跑的项目数」分摊模型通道。

    单个项目默认会一次占满 2 个模型并发；若另一个项目也在生成，它会一直等在
    进程级模型闸门上，从界面上看就像「被暂停」。这里保证每个在跑项目至少拿到
    一条通道，使多个项目真正并行推进，而不是互相饿死。
    """
    limit = _model_channel_limit()
    try:
        async with pool.acquire() as conn:
            others = await conn.fetchval(
                "select count(distinct project_id) from project_generation_runs "
                "where status in ('queued','planning','running') "
                "and project_id::text <> $1 "
                "and heartbeat_at > now() - interval '120 seconds'",
                project_id,
            )
    except Exception as exc:  # noqa: BLE001 分摊查询失败不能拖垮生成
        logger.warning("[正文生成] 并发分摊查询失败，按独占通道处理：%s", exc)
        return limit
    active_projects = max(1, int(others or 0) + 1)
    return max(1, limit // active_projects)


def _shared_ledger(ledger_text: str) -> str:
    """跨章不变的「全书统一事实」——单独成一条 user 消息才能命中前缀缓存。

    默认原样保留完整台账（小节配额只用于病态超长时的兜底），保证任何规模下
    都不会因为预算把某节整段丢掉。
    """
    value = str(ledger_text or "")
    if len(value) > SHARED_LEDGER_MAX_CHARS:
        value = truncate_ledger(value, SHARED_LEDGER_MAX_CHARS) + _LEDGER_OVERSIZE_MARK
    return "全书统一事实与承诺口径（逐字遵守，不把不同对象混为一谈）：\n" + value


def _trim_ceiling(target_words: int) -> int:
    """超过这个字数就认为章节失控，需要收敛。"""
    target = max(0, int(target_words or 0))
    if target <= 0:
        return 0
    return max(int(target * WORD_LIMIT_RATIO), target + WORD_LIMIT_SLACK)


def _accept_condensed(candidate: str, current: str,
                      validate: Callable[[str], list[str]],
                      target_words: int = 0) -> tuple[bool, str]:
    """收敛稿是否可用：必须更短、不能变成摘要、且仍通过质检与合规校验。

    下限按**目标字数**算而不是按原稿算：原稿严重超标（850 字目标写出 1.1 万字）时，
    按原稿 35% 兜底会把本来正确的收敛稿误判成摘要。
    宁可保留偏长的原稿，也不能为了字数丢掉参数、承诺或合规口径。
    """
    text = str(candidate or "").strip()
    cur_words = len(re.sub(r"\s", "", str(current or "")))
    new_words = len(re.sub(r"\s", "", text))
    target = max(0, int(target_words or 0))
    floor = max(200, int(target * 0.5)) if target > 0 else int(cur_words * 0.35)
    if not text:
        return False, "收敛稿为空"
    if new_words >= cur_words:
        return False, "收敛稿没有变短"
    if new_words < floor:
        return False, "收敛稿过短（%d 字 < 下限 %d 字），疑似摘要而非正文" % (new_words, floor)
    issues = validate(text)
    if issues:
        return False, "；".join(issues[:3])
    return True, ""


def _condense_prompt(title: str, text: str, target: int, current: int) -> str:
    """正文超出目标篇幅时的收敛指令：只删重复与铺垫，不删事实。"""
    return (
        f"以下《{title}》正文约 {current} 字，远超本节目标篇幅约 {target} 字，需要收敛改写。\n"
        f"请压缩到 **{target} 字左右**（不超过 {int(target * 1.2)} 字）。\n"
        "压缩纪律（必须遵守）：\n"
        "1. 删掉重复表述、空话套话、泛泛的背景铺垫、与本节职责无关的内容、对其它章节的复述；\n"
        "2. **不得删除**任何具体参数、数值、单位、材质牌号、标准号、数量、工期、质保期限、"
        "服务承诺、评分项响应、表格中的行、{{待填:字段名称}} 占位；\n"
        "3. 保留原有的小标题层级与编号体系；\n"
        "4. 不要新增原文没有的事实、不要编造数据，也不要写成摘要或提纲。\n"
        "只输出收敛后的完整正文，不要解释。\n\n【现有正文】\n" + str(text or "")
    )


def _expand_prompt(title: str, text: str, target: int, current: int) -> str:
    """字数不足时的定向扩写指令。"""
    return (
        f"以下《{title}》正文只有约 {current} 字，低于本节目标篇幅约 {target} 字。\n"
        "请在**不新增无依据事实**的前提下补足篇幅：补充已有依据可支撑的实施步骤、"
        "分工、控制点、验收方式、风险应对与保障措施，把空泛表述改写成可核查的具体做法。\n"
        "不得编造专利号、证书编号、业绩、人员姓名、报价或量化承诺；"
        "资料缺失处继续使用 {{待填:字段名称}} 占位。\n"
        "只输出补足后的完整正文，不要解释。\n\n【现有正文】\n" + str(text or "")
    )


# ══════════════════ 文档级后处理（落库后、自检前）══════════════════

async def _apply_chapter_text(pool: Any, chapter: dict[str, Any], text: str) -> None:
    """把后处理改过的正文写回内存章节与数据库（写库失败只告警，不拖垮任务）。"""
    chapter["content"] = text
    chapter["words"] = len(re.sub(r"\s", "", text))
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "update project_chapters set content=$2, words=$3, updated_at=now() "
                "where id::text=$1",
                str(chapter.get("id") or ""), text, chapter["words"],
            )
    except Exception as exc:  # noqa: BLE001 - 落库失败不能让一次生成任务崩掉
        logger.warning("[文档后处理] 章节 %s 写库失败：%s", chapter.get("title"), exc)


def _repair_join_residue(text: str) -> tuple[str, list[str]]:
    """落库前的拼接修复：被换行拆开的表格行拼回整行 + 段末半句补成完整句。

    两个动作都是纯文本、幂等、绝不抛异常（异常时原文原样返回），所以整册后处理与单章
    重跑两条路径可以放心各调一次。顺序不能反：先把行拼成整行，再说句子——半句补全只看
    段落行，表格行它本来就不碰。
    """
    from zhibiao.core.table_join import complete_truncated_paragraphs, join_split_table_rows

    body = str(text or "")
    if not body.strip():
        return body, []
    joined, notes = join_split_table_rows(body)
    completed, sentence_notes = complete_truncated_paragraphs(joined)
    return completed, notes + sentence_notes


async def _document_postfix(pool: Any, chapters: list[dict[str, Any]],
                            tender_text: str) -> list[str]:
    """落库后的整册后处理：① 代理商身份表述 ② 信誉查询渠道条数 ③ 防重复计价提示
    ④ 半截「对照/响应」表 ⑤ 联合体/制造商授权的「本项不适用」说明 ⑥ 跨章重复压缩。

    为什么放在整册层面：半截表要和「后文那张完整表」比才知道删还是补；代理口径要看
    招标原文。单章生成时已经各做一次，这里是兜底（续跑、单章重跑、旧任务留下的正文
    也会在收尾时归一，用户不必为了这类机械问题重新生成整本）。

    返回处理记录（给日志/任务消息用）；没命中任何一条就返回空列表，不写库。
    """
    from zhibiao.core.table_completeness import reconcile_response_tables

    notes: list[str] = []
    updated: dict[str, str] = {}
    for chapter in chapters:
        content = str(chapter.get("content") or "")
        if not content.strip():
            continue
        title = str(chapter.get("title") or "").strip()
        fixed, agency_notes = suppress_agency_assumptions(content, tender_text)
        if agency_notes:
            notes.append("「%s」%s" % (title, agency_notes[0]))
        # 信誉/信用查询证明：招标点名几个渠道就该有几行文字、几个图框（旧正文常漏写渠道，
        # 或图框数与图框说明互相矛盾），按招标原文的渠道清单整节重建。
        fixed, credit_note = ensure_credit_channels(fixed, tender_text, title)
        if credit_note:
            notes.append("「%s」%s" % (title, credit_note))
        # 同级格式小节一个都不能少：资格审查资料这类父节下（一）~（八）是招标人规定的
        # 法定格式，结构化直出只留下检出表格的那几节，没有表格的小节整段消失。
        # 这里按招标原文的小节清单补框架 + 占位框（幂等；已有小节逐字不动）。
        # require_anchor=True：只补"正文里已经出现过原文小节"的章节，避免把八个小节
        # 硬塞进一篇与格式章同名的模型撰写正文。
        from zhibiao.core.format_subsections import ensure_format_subsections
        fixed, subsection_added = ensure_format_subsections(
            fixed, tender_text, title, require_anchor=True)
        if subsection_added:
            notes.append("「%s」补齐招标格式小节 %d 个：%s" % (
                title, len(subsection_added), "、".join(subsection_added)))
        # 招标文件写明「不接受联合体投标」/「不接受代理商投标」时，《联合体协议书》章末与
        # 《制造商授权书》小节末要有一行**加粗**说明「本项不适用」：这两章是招标文件规定的
        # 格式，删了是形式评审缺项，假装在填则是让投标人去凑本项目不可能有的材料。
        # 判定与插入位置全部在 legal_forms（原文允许时一个字都不加；已加过不再加），
        # 放在小节补全**之后**：小节结构定稿了，注才落在真正的小节末尾。
        from zhibiao.core.legal_forms import ensure_applicability_note
        fixed, applicability_note = ensure_applicability_note(fixed, tender_text, title)
        if applicability_note:
            notes.append("「%s」%s" % (title, applicability_note))
        # 「永磁…滚筒」与「传动滚筒」同表并列：表后补一行防重复计价提示（表格行不动）。
        fixed, pricing_note = add_drum_pricing_note(fixed)
        if pricing_note:
            notes.append("「%s」%s" % (title, pricing_note))
        if fixed != content:
            updated[str(chapter.get("id") or "")] = fixed
    # 对照表核对在同一次结果上继续改（代理口径已改过的正文作为输入）
    probe = [dict(chapter, content=updated.get(str(chapter.get("id") or ""),
                                               chapter.get("content")))
             for chapter in chapters]
    try:
        for edit in reconcile_response_tables(probe, tender_text):
            chapter_id = str(edit.get("chapter_id") or "")
            if not chapter_id:
                continue
            updated[chapter_id] = str(edit.get("content") or "")
            notes.append("「%s」%s" % (str(edit.get("title") or "").strip(),
                                      str(edit.get("detail") or "")))
    except Exception as exc:  # noqa: BLE001 - 表格核对失败只跳过这一项
        logger.warning("[文档后处理] 对照表核对失败，已跳过：%s", exc)
    # 表格行拼接 + 段末半句补全（幂等纯文本）：10.2.10 的「一行被写成两行、后半段甩到表外」
    # 与 12.2.7 的「…安全、质量」断尾都属于段中/表内断点，章末断尾判定覆盖不到。
    # 顺序必须排在上一段对照表核对之后：先删/补半截表再拼残行，否则表里的行号会漂。
    for chapter in chapters:
        chapter_id = str(chapter.get("id") or "")
        # 只修「已生成」的正文：失败稿（状态不是已生成）里那句提示语不是投标正文，
        # 往里补宾语既没意义，还会把失败稿写回去（实测单章失败路径因此被误判成功）。
        status = str(chapter.get("status") or "").strip()
        if status and status != "已生成":
            continue
        current = str(updated.get(chapter_id, chapter.get("content") or ""))
        if not current.strip():
            continue
        repaired, join_notes = _repair_join_residue(current)
        if repaired == current:
            continue
        updated[chapter_id] = repaired
        for note in join_notes[:4]:
            notes.append("「%s」%s" % (str(chapter.get("title") or "").strip(), note))
    # 跨章重复压缩（B 档）：同一口径在若干章里近乎逐字复述时，只留专职章那一份，
    # 其余章换成一行中性引用。必须看整册才能判「哪一章是专职章」，所以放在这里。
    # 排在拼接修复**之后**：先让每段句子完整，再判重复，否则半句永远匹配不上。
    # 纯文本、幂等（第二次运行 0 命中），可用 config.CROSS_CHAPTER_DEDUP 关闭。
    try:
        from zhibiao.core.cross_chapter_dedup import compress_cross_chapter_duplicates

        probe = [dict(chapter, content=updated.get(str(chapter.get("id") or ""),
                                                   chapter.get("content")))
                 for chapter in chapters]
        dedup_updates, dedup_records = compress_cross_chapter_duplicates(probe, tender_text)
    except Exception as exc:  # noqa: BLE001 - 压缩失败只跳过这一项，绝不改坏正文
        logger.warning("[文档后处理] 跨章重复压缩未完成，已跳过：%s", exc)
        dedup_updates, dedup_records = {}, []
    if dedup_updates:
        titles = {str(chapter.get("id") or ""): str(chapter.get("title") or "").strip()
                  for chapter in chapters}
        per_chapter: dict[str, int] = {}
        for record in dedup_records:
            key = str(record.get("chapter_id") or "")
            per_chapter[key] = per_chapter.get(key, 0) + 1
        for chapter_id, text in dedup_updates.items():
            updated[chapter_id] = text
            notes.append("「%s」跨章重复压缩 %d 处"
                         % (titles.get(chapter_id, ""), per_chapter.get(chapter_id, 1)))
    for chapter in chapters:
        chapter_id = str(chapter.get("id") or "")
        if chapter_id in updated:
            await _apply_chapter_text(pool, chapter, updated[chapter_id])
    return notes
