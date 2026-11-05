"""投标检查的确定性校验层（不依赖大模型，逐条可复核）。

为什么要有这一层：
- 之前 21 项规则全靠大模型「看一眼节选」判断，凡是节选里没有的（投标函、营业执照、
  签章页）一律给「待复核」，用户看到的是一屏「严重 10 项 / 中度 10 项」，实际一条
  结论都没有——这就是「摆设」的由来。
- 这层做两件事：**先判适用范围**（技术分册不该被商务规则扣分），**再跑规则化校验**
  （编号、条目、覆盖、数量这些本来就能算准的东西不该问模型）。

原则：
- 算得准才给「通过 / 必须修改」，算不准一律返回 None，交给上层（AI 或人工），
  这里的 None 不是「通过」。
- 唯一的例外是「数据缺口」：规则要用的数据本身没取到（例如 C11 拿不到章号），
  此时给「通过 + 写明本次未能核对」，绝不把我们自己的数据缺口算成投标人的错误。
"""
from __future__ import annotations

import re
from typing import Any

from zhibiao.core.chapter_rules import SUBHEADING_RE, heading_number

# 完整投标文件的「商务脊梁」：出现其中任意一章，才认为这一册包含商务/资格部分。
BUSINESS_SPINE = ("投标函", "开标一览表", "投标报价", "分项报价", "法定代表人", "授权委托",
                  "营业执照", "保证金", "资格审查", "资格证明")

# 只有在「完整投标文件」里才成立的规则；技术分册成册时这些规则不适用。
BUSINESS_RULES = frozenset({"C01", "C02", "C03", "C04", "C05", "C17", "C19"})

# 代码类编号：M1401000156004970001 / JSH2026-375 / XJ2026-01 这类
_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,6}\d{6,}(?:-\d{1,6})?)(?![0-9A-Za-z])")

# 「这一章已经有正文」的下限：占位符、空白、一句话都算没写，避免把空章节当成已响应。
_WRITTEN_MIN = 50

# 照录类章节：正文不是投标人写的，而是招标文件规定的格式原文（投标函/授权委托书/
# 偏差表/报价表/承诺书…）或材料插入占位。它们的编号、措辞、表格形状由招标人规定，
# 按「作者视角」的写作规则去判（编号体系、逐条对照表、篇幅）只会把抄得对的判成错。
# 判据与生成侧一致：只认 generation_runner 写进 project_chapters.source 的那几个前缀。
_QUOTED_SOURCE_PREFIXES = ("格式文书", "招标文件格式原文", "规则占位")


def _is_quoted(chapter: dict[str, Any]) -> bool:
    source = str(chapter.get("source") or "")
    return source.startswith(_QUOTED_SOURCE_PREFIXES)


def _has_body(chapter: dict[str, Any]) -> bool:
    if not isinstance(chapter, dict):      # 混进脏行（None / 字符串）时不算「有正文」
        return False
    return len(str(chapter.get("content") or "").strip()) >= _WRITTEN_MIN


def _mostly_written(chapters: list[dict[str, Any]]) -> bool:
    """这一册是不是基本成稿了（至少 60% 的章节有正文）。

    没成稿时，「某某章节还是空的」是必然状态、不是缺陷；此时相关规则应交给
    「待相应内容生成后复核」，不能报成必须修改。
    """
    if not chapters:
        return False
    written = sum(1 for chapter in chapters if _has_body(chapter))
    return written >= 2 and written * 10 >= len(chapters) * 6


def _titles(chapters: list[dict[str, Any]]) -> str:
    return " ".join(str(c.get("title") or "") for c in chapters)


def _body(chapters: list[dict[str, Any]]) -> str:
    return "\n".join(str(c.get("content") or "") for c in chapters)


def document_scope(chapters: list[dict[str, Any]]) -> str:
    """这一册是「完整投标文件」、「技术分册」还是「看不出来」。

    只看**有正文**的章节标题：目录里有「投标函」但一个字都没写，说明这一册其实只是
    技术分册（商务部分留待另册），不能因此按完整投标文件去套商务规则。正文里提到
    「投标函」同样不算——只有该章自己有内容才算这一册包含它。
    """
    written = [c for c in chapters if _has_body(c)]
    titles = _titles(written or chapters)
    if any(key in titles for key in BUSINESS_SPINE):
        return "complete"
    if re.search(r"技术|方案|参数|指标|服务|质保|实施|进度|质量|安全|培训", titles):
        return "technical"
    return "unknown"


def applicability(cid: str, scope: str) -> tuple[bool, str]:
    """该规则对当前这一册是否适用。返回 (适用?, 不适用时的原因)。"""
    if cid in BUSINESS_RULES and scope == "technical":
        return False, ("本册为技术分册，不含《投标函》《报价表》及各类资格证明，"
                       "该项由商务册承担；本项不计入本次结论，须与商务册合并后复核")
    return True, ""


def _dim_value(dims: list[dict[str, Any]], *names: str) -> str:
    for item in dims or []:
        name = str(item.get("name") or "")
        if name in names:
            value = str(item.get("value") or "").strip()
            if value and "未识别" not in value and item.get("hit", True):
                return value
    return ""


def _items(cid: str, status: str, hit: str, detail: str, evidence: str = "",
           **extra: Any) -> dict[str, Any]:
    """一条确定性结论。`extra` 用来带章节定位（chapter_id / chapter_title / chapter_seq）。"""
    item = {"status": status, "hit": hit, "detail": detail,
            "evidence": evidence, "engine": "确定性校验"}
    for key, value in extra.items():
        if value not in (None, ""):
            item[key] = value
    return item


def _check_c08(chapters: list[dict[str, Any]], source_text: str) -> dict[str, Any] | None:
    """技术要求逐条响应：拿招标条目去核对参数/对照/偏离类章节的对照表。

    「逐条响应」有两种合规写法，必须都认，否则自己生成的合格正文会被自己判错：
    ① 用逐条对照表（表格行数覆盖条目数）—— 与生成门禁用同一个函数
       `completeness.tech_item_gaps`、同一个章节判据 `ITEM_TABLE_CHAPTER`；
    ② 在技术响应正文里逐条写到（没有对照表时按**条目本体是否出现**核对，
       不按措辞、不按表形判）。
    """
    from zhibiao.core.completeness import (ITEM_TABLE_CHAPTER, md_tables, tech_item_gaps,
                                       tech_items)
    # 边界条款（「由矿方自行配套」这类）不是要逐条填表的条目，数行数时排除，
    # 否则对照表按 13 条写、这里按 15 条判，会凭空多出 2 条「漏项」。
    items = [item for item in tech_items(source_text or "") if not item.get("boundary")]
    if len(items) < 2:
        # 条目太少时「漏一条」与「合并成一行」无法区分，不硬判
        return None
    # 照录格式章（投标函/报价表/承诺书…）里出现「技术要求」字样不代表它是技术响应章
    written = [c for c in chapters
               if str(c.get("content") or "").strip() and not _is_quoted(c)]
    targets = [c for c in written if ITEM_TABLE_CHAPTER.search(str(c.get("title") or ""))]
    if not targets:
        has_tech = [c for c in written if re.search(r"技术", str(c.get("title") or ""))]
        if not has_tech:
            return None
        missing = _items_missing_from_text(items, "\n".join(str(c.get("content") or "") for c in written))
        if not missing:
            return _items("C08", "通过", "技术响应章节",
                          "招标文件 %d 条编号技术要求均已在正文逐条响应"
                          "（本册未单列对照表，按条目本体核对）" % len(items))
        return _items("C08", "必须修改", "技术章节",
                      "招标文件有 %d 条编号技术要求，正文既没有参数/指标对照表，"
                      "也未逐条写到其中 %d 条（如 %s）" % (len(items), len(missing), missing[0]),
                      "未响应的技术要求条目：%s" % "、".join(missing[:6]))
    gaps: list[str] = []
    hit_title = ""
    for chapter in targets:
        found = tech_item_gaps(source_text or "", str(chapter.get("content") or ""),
                               str(chapter.get("title") or ""))
        if found:
            hit_title = str(chapter.get("title") or "")
            gaps.extend(found)
    if not gaps:
        # 条目数不足 5 时 completeness 不判（避免误伤合并行）；这里补一轮直接数行：
        # 对照表行数少于条目数 - 1 就是漏项，证据是行数与条目清单本身。
        # 关键：按**整册**最好的一张对照表判覆盖，不是逐章各判一次 —— 同一册里既有
        # 「逐条对照表（列全了）」又有一张「响应要点小表」时，逐章判会把后者当成漏项。
        best = 0
        header_best = ""
        loose = 0   # 表头没写「对照/响应/指标」的表（如「参数名称 / 招标要求 / 投标值」）兜底
        for chapter in targets:
            content = str(chapter.get("content") or "")
            for table in md_tables(content):
                rows = len(table["rows"])
                header = " ".join(str(cell) for cell in table["header"])
                if re.search(r"对照|响应|指标", header):
                    if rows > best:
                        best, header_best = rows, header
                        hit_title = str(chapter.get("title") or "")
                elif rows > loose:
                    loose = rows
                    if not hit_title:
                        hit_title = str(chapter.get("title") or "")
        if not best:
            best, header_best = loose, (header_best or "参数表")
        if best and best < len(items) - 1:
            gaps.append("对照表「%s」只列了 %d 行，招标技术要求共 %d 条（缺 %d 条）"
                        % (header_best[:14], best, len(items), len(items) - best))
        elif not best:
            # 目标章节里根本没有表：退到「正文是否逐条写到」这一层，别拿「没有表」当通过
            missing = _items_missing_from_text(
                items, "\n".join(str(c.get("content") or "") for c in targets))
            if missing:
                gaps.append("对照章节里没有参数/指标表格，正文也未逐条写到其中 %d 条（如 %s）"
                            % (len(missing), missing[0]))
    if gaps:
        return _items("C08", "必须修改", hit_title or "参数对照章节",
                      "%d 处未逐条响应。%s" % (len(gaps), gaps[0]),
                      gaps[0][:200])
    return _items("C08", "通过", hit_title or "参数/对照章节",
                  "已按招标文件 %d 条编号技术要求逐条核对，对照章节无漏项" % len(items))


def _items_missing_from_text(items: list[dict[str, Any]], body: str) -> list[str]:
    """哪些技术要求条目在正文里根本找不到（按条目本体比，不按措辞）。

    条目本体是招标原文里「1、驱动方式：…」去掉编号后的前 12 个字（`_clean_head`），
    它是采购人自己的用词，投标人写响应时必须复述到，所以可以机械核对。
    """
    flat = re.sub(r"\s+", "", str(body or ""))
    missing: list[str] = []
    for item in items:
        head = str(item.get("head") or "").strip()
        if not head:
            continue
        if re.sub(r"\s+", "", head) not in flat:
            missing.append("%d %s" % (int(item.get("index") or 0), head))
    return missing


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(text or ""))
    return {clean[index:index + 2] for index in range(len(clean) - 1)}


# 评分项里能指认章节的概念词。只有这些词在标题里出现，才认为「这一项有对应章节」，
# 不用同义词扩展（那会把「供货业绩」命中到「技术参数对照表」上，判成假通过）。
_CONCEPTS = ("技术", "方案", "参数", "指标", "结构", "配置", "性能", "工艺", "设备",
             "服务", "质保", "售后", "运维", "培训", "实施", "进度", "工期", "质量",
             "安全", "验收", "测试", "业绩", "资质", "资格", "报价", "价格", "总价",
             "费用", "成本", "履约", "信誉", "财务", "备件", "包装", "运输", "图纸",
             "资料", "监测", "控制", "节能", "环保")


def _covered_by(name: str, titles: list[str]) -> bool:
    """评分项是否真有对应章节。

    先用概念词（技术/服务/质保/业绩…）在标题里找，找不到再用名称本身的二字切片兜底
    （重合 ≥2 个才算）。这样「对投标人技术服务和质保期服务能力的评价」能被
    「售后服务与运维保障」命中，而「供货业绩」不会被「技术参数对照表」误判为已覆盖。
    """
    concepts = {word for word in _CONCEPTS if word in str(name or "")}
    if concepts:
        for title in titles:
            if title and any(word in title for word in concepts):
                return True
        return False
    name_bigrams = _bigrams(name)
    if not name_bigrams:
        return False
    for title in titles:
        if not title:
            continue
        if len(name_bigrams & _bigrams(title)) >= 2:
            return True
    return False


_TECH_ITEM_HINT = re.compile(r"技术|方案|参数|指标|工艺|结构|性能|配置|服务|质保|售后|培训|实施|进度|质量|安全|验收|测试|培训")


def _check_c10(chapters: list[dict[str, Any]], scores: list[dict[str, Any]],
               scope: str = "unknown") -> dict[str, Any] | None:
    """评分项内容全覆盖：评分项名称与章节标题做概念词匹配。

    判「必须修改」的门槛很严：只有**目录里已经有对应章节、但那一章还没写正文**时才报
    （此时确实是漏写）。仅仅目录里没有这个标题，只能说明「这一册没打算写它」，
    属于编排问题，交人工决定（技术分册里业绩/资质/报价本来就由商务册承担）。
    """
    items = [s for s in (scores or []) if str(s.get("name") or "").strip()]
    if not items:
        return None
    written = [c for c in chapters if _has_body(c)]
    if not written or not _mostly_written(chapters):
        # 正文还没成稿：目录对不对得上此时无意义，交给「待生成后复核」，不制造假问题
        return None
    written_titles = [str(c.get("title") or "") for c in written]
    missing = [str(s["name"]) for s in items if not _covered_by(str(s["name"]), written_titles)]
    if not missing:
        return _items("C10", "通过", "章节目录", "评分项 %d 个全部有对应章节" % len(items))
    # 目录里有个「大致对得上」的章节但一个字没写 → 这才是真漏写
    empty_hits: list[str] = []
    for name in missing:
        grams = _bigrams(name)
        for chapter in chapters:
            title = str(chapter.get("title") or "")
            if not title or len(grams & _bigrams(title)) < 1:
                continue
            if not _has_body(chapter):
                empty_hits.append(name)
                break
    if empty_hits:
        return _items("C10", "必须修改", "章节目录",
                      "有 %d 个评分项对应的章节还是空的：%s"
                      % (len(empty_hits), "、".join(empty_hits[:6])),
                      "未覆盖的评分项：%s" % "、".join(empty_hits[:6]))
    tech_missing = [name for name in missing if _TECH_ITEM_HINT.search(name)]
    if not tech_missing and scope == "technical":
        return _items("C10", "待复核", "章节目录",
                      "有 %d 个评分项在本册找不到对应章节，且都属于商务/资格口径，"
                      "可能由商务册承担：%s" % (len(missing), "、".join(missing[:6])),
                      "需确认这些评分项是否在本册响应：%s" % "、".join(missing[:6]))
    return _items("C10", "待复核", "章节目录",
                  "有 %d 个评分项在目录里找不到对应章节：%s。可能是本册有意不写（商务册承担），"
                  "也可能是漏排，需人工确认" % (len(missing), "、".join(missing[:6])),
                  "确认这些评分项由哪一册响应，或在目录里补章节：%s" % "、".join(missing[:6]))


# 编号体系最多 4 层（a.b.c.d）。成稿里出现第 5 层，说明父编号之后又拼了一次序号。
MAX_NUMBER_DEPTH = 4

# 章节标题行首自带编号（「6.3 服务安全保证措施」「10 技术性能」）：它就是这一章的章号。
# 数字后面必须跟空白，避免把「3.5 米宽输送机」这类正文型标题当成章号。
_TITLE_NUMBER_RE = re.compile(r"^[ \t\u3000]*(\d{1,3}(?:[.．]\d{1,3})*)[ \t\u3000]+")


def _number_segments(value: Any) -> list[int] | None:
    """「6.3.3」→ [6, 3, 3]；不是纯数字点分编号返回 None（算不准就不判，绝不猜）。"""
    parts = str(value or "").replace("．", ".").strip().strip(".").split(".")
    segments: list[int] = []
    for part in parts:
        part = part.strip()
        if not part.isdigit():
            return None
        segments.append(int(part))
    return segments or None


def _number_text(segments: list[int]) -> str:
    return ".".join(str(segment) for segment in segments)


def _outline_numbers(chapters: list[dict[str, Any]]) -> dict[str, str]:
    """按大纲算每一章的章号（章节 id → 「6.3」），与导出侧、修复侧同一个函数。

    算不出来（缺 id / 畸形 level）的章节不进表：没有章号就不猜，只做层数与孤立判定。
    """
    try:
        from zhibiao.core.chapter_rules import section_numbers
        raw = section_numbers([c for c in (chapters or []) if isinstance(c, dict)])
    except Exception:  # noqa: BLE001 - 大纲算不出来时降级，不让 C11 整体失效
        return {}
    out: dict[str, str] = {}
    for key, value in (raw or {}).items():
        if _number_segments(value) is not None:
            out[str(key)] = str(value)
    return out


# 章号来源：决定「能不能拿这个章号当基准判编号」。
# 线上真实目录里 seq 只是全局行序（二级章的 seq 更不是章号）。`checks_api.load_corpus`
# 现在会把 id 一并取出来，大纲编号因此可用；但导入的标书（`project_bid_docs` 里解析出的
# 章节没有 id）以及任何只传 seq 的调用方仍然拿不到章号，`_outline_numbers()` 为空。
# 用户实测：成稿编号本身是对的（6.1.1 在章 6 下、7.1.1 在章 7 下），检查器把 seq 当章号
# （或干脆没有章号）后自己算错，一边说「父编号找不到 / 应为 6.1」，一边说「未能定位章节」
# ——等于承认没有依据却判了「必须修改」。所以能当基准的只有下面三种来源，「仅来自 seq」
# 或「章号为空」时一律视为无法核对（不判前缀、不判同级连续、不判孤立），由调用方写成
# 「本次未能核对章节编号」。
NUMBER_SOURCE_OUTLINE = "大纲编号"
NUMBER_SOURCE_TITLE = "标题自带编号"
NUMBER_SOURCE_SEQ = "章序号"
# seq 与正文首条「章号.N」互证（两个独立来源一致）时才算数：整章只用两层编号（a.b）时，
# 第一条就是「本章号 + 序号」的直接证据；一旦出现 3 段以上编号，seq 只是行序，不能当章号。
NUMBER_SOURCE_CORROBORATED = "章序号与正文首条编号互证"
RELIABLE_NUMBER_SOURCES = frozenset({NUMBER_SOURCE_OUTLINE, NUMBER_SOURCE_TITLE,
                                     NUMBER_SOURCE_CORROBORATED})

# 不可核对时的固定说明：缺章号是**我们的数据缺口**，不是投标人的编号错误，
# 所以给「通过 + 写明未核对」，绝不报「父编号找不到」、绝不判「必须修改」。
UNVERIFIED_NUMBER_NOTE = "本次未能核对章节编号（缺少章节编号数据）"


def _chapter_number_candidates(chapter: dict[str, Any],
                               outline: dict[str, str]) -> list[tuple[str, str]]:
    """这一章可能的 (章号, 来源)，按可信度排序：大纲编号 → 标题自带编号 → seq。

    seq 只对一级章有意义（线上真实目录 level∈{1,2} 交替，二级章的 seq 是全局行序，
    不是章号），因此只在 level<=1 时进候选。
    来源必须一路带到判据里：seq 只能用于本层编号形状校验，**不能作为「编号孤立」的依据**。
    """
    candidates: list[tuple[str, str]] = []
    key = str(chapter.get("id") or "")
    if key and key in outline:
        candidates.append((outline[key], NUMBER_SOURCE_OUTLINE))
    matched = _TITLE_NUMBER_RE.match(str(chapter.get("title") or ""))
    if matched:
        candidates.append((matched.group(1).replace("．", "."), NUMBER_SOURCE_TITLE))
    level = chapter.get("level")
    try:
        shallow = int(level or 1) <= 1
    except (TypeError, ValueError):
        shallow = False
    seq = chapter.get("seq")
    if shallow and str(seq if seq is not None else "").strip().isdigit():
        candidates.append((str(int(str(seq).strip())), NUMBER_SOURCE_SEQ))
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for number, source in candidates:
        if number and number not in seen and _number_segments(number) is not None:
            seen.add(number)
            unique.append((number, source))
    return unique


def _pick_chapter_number(chapter: dict[str, Any], outline: dict[str, str],
                         first_number: str) -> tuple[str, str]:
    """定这一章的 (章号, 来源)：优先取与正文首条小标题同源的候选。

    章号可能来自大纲，也可能只写在标题里（「6.3 服务安全保证措施」）；正文里
    第一条 `## 6.3.1 …` 本身就是「本章章号是 6.3」的直接证据。三者都拿不到时返回 ("", "")。
    返回来源而不是只返回章号，是因为调用方要据此判断**这个章号能不能用来判编号孤立**。
    """
    candidates = _chapter_number_candidates(chapter, outline)
    if first_number:
        for number, source in candidates:
            if first_number.startswith(number + "."):
                return number, source
    return candidates[0] if candidates else ("", "")


def _chapter_label(title: str, chapter_number: str) -> str:
    """报点用的章节标题：章号已经单独写在前面的，标题里不再重复一遍。"""
    text = str(title or "").strip() or "未命名章节"
    if not chapter_number or not text.startswith(chapter_number):
        return text
    rest = text[len(chapter_number):]
    if rest[:1].isdigit():          # 「6.31 …」不是「6.3」的标题，原样保留
        return text
    cleaned = rest.lstrip(" \t\u3000.．、:：)）")
    return cleaned or text


def _chapter_where(title: str, chapter_number: str) -> str:
    """报点里的章节称呼：有章号写「第 6.3 节「…」」，没有就只写标题。"""
    if chapter_number:
        return "第 %s 节「%s」" % (chapter_number, _chapter_label(title, chapter_number))
    return "「%s」" % title


def _chapter_locator(chapter: dict[str, Any]) -> dict[str, Any]:
    """命中章节的定位信息（形态与 `checks_api.locate_chapter` 一致，界面据此跳到该章正文）。

    取不到标题就不填：宁可让界面显示「未定位」，也不编一个章节出来。
    """
    title = str(chapter.get("title") or "").strip()
    if not title:
        return {}
    try:
        seq = int(chapter.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return {"chapter_id": str(chapter.get("id") or ""),
            "chapter_title": title, "chapter_seq": seq}


def _hit_with_chapter(chapter: dict[str, Any]) -> str:
    """「命中位置」里带上**原始章标题**，界面才能定位到该章正文。

    `checks_api.locate_chapter` 是按「章标题是否出现在命中位置里」做最长匹配的：
    这里如果只写「目录 / 章节标题」，就会永远显示「未能定位到具体章节」。
    """
    title = str(chapter.get("title") or "").strip()
    return "目录 / 章节标题：「%s」" % title if title else "目录 / 章节标题"


def _numbered_headings(content: str) -> list[tuple[int, str, str]]:
    """正文里每个内嵌小标题的 (Markdown 层级, 编号类型, 编号本体)，跳过代码围栏。

    Markdown 层级决定父子关系：`### 6.3.3.1` 的上一个 `##` 标题是它的父标题。
    """
    out: list[tuple[int, str, str]] = []
    in_fence = False
    for line in str(content or "").splitlines():
        if re.match(r"^[ \t]*(?:```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        matched = SUBHEADING_RE.match(line)
        if not matched:
            continue
        kind, value = heading_number(matched.group(2))
        if kind:
            out.append((len(matched.group(1)), kind, value))
    return out


def _deeply_numbered(items: list[tuple[int, str, str]]) -> bool:
    """本章正文有没有 3 段及以上的编号（a.b.c）。"""
    for _depth, kind, value in items:
        if kind == "cjk":
            continue
        segments = _number_segments(value)
        if segments and len(segments) >= 3:
            return True
    return False


def _corroborated_number(chapter: dict[str, Any],
                         outline: dict[str, str]) -> tuple[str, str]:
    """seq 与正文首条「章号.N」互证时，把 seq 升格成可用的章号；否则返回 ("", "")。

    为什么允许这一步：导入的标书章节没有 id（解析结果里没有章节主键），大纲编号算不出来，
    章号常常只能来自 seq。此时若正文整章只用两层编号（`3.1` / `3.2`），第一条就是
    「本章号 + 序号」的直接证据，且与 seq 一致 —— **两个独立来源互相印证**，可以当基准。
    `## 14.1` 写在 seq=34 的章里就不满足互证（14≠34），仍按「无法核对」处理。

    一旦出现 3 段以上编号就退回不可核对：那时作者可能按「上级章号.子章号.序号」写
    （`6.1.1` 挂在章 6 下、`12.5.1` 挂在 12.5 节下），seq 只是行序，拿它当章号必算错。
    """
    items = _numbered_headings(str(chapter.get("content") or ""))
    if not items or _deeply_numbered(items):
        return "", ""
    first = items[0][2] if items[0][1] != "cjk" else ""
    number, source = _pick_chapter_number(chapter, outline, first)
    if source != NUMBER_SOURCE_SEQ or not number:
        return "", ""
    segments = _number_segments(first) or []
    if len(segments) == 2 and str(segments[0]) == str(number):
        return number, NUMBER_SOURCE_CORROBORATED
    return "", ""


def _numbering_problems(title: str, chapter_number: str,
                        items: list[tuple[int, str, str]],
                        number_source: str = NUMBER_SOURCE_OUTLINE) -> list[str]:
    """一章正文内嵌小标题的编号判据：父子各一层 + 同级连续 + 层数上限 + 重号。

    只看编号本身的形状与相对位置，不猜作者意图；报出的是「在哪、现在写成什么、
    按父链应为哪个编号」，不做合规评价。

    `number_source` 决定**章号能不能当基准**：章号仅来自 seq（未互证）或为空时，
    「应为 X.Y」「同级编号从 1 起连续」这两条都以我们猜的章号为基准，报出去等于让
    投标人按我们算错的编号改正文（用户实测误报：「第 6 节…编号 6.1.1 应为 6.1」）。
    此时只判**正文自身就能证明**的层数上限；父标题来自正文自己的编号（`## 6.3` 下的
    `### 6.3.3.1`）时照常判，那是以文档里的编号为准，不依赖章号。
    """
    where = _chapter_where(title, chapter_number)
    # 章号能当基准的唯一条件就是「来源可靠」。seq（行序）与空章号都不行：
    # 实测 seq=34 的章正文写着 14.1，拿 34 当章号就会报「编号前缀与本章章号 34 不一致」。
    anchor_reliable = (bool(chapter_number)
                       and number_source in RELIABLE_NUMBER_SOURCES)
    problems: list[str] = []
    seen: set[str] = set()
    stack: list[tuple[int, str]] = []          # [(Markdown 层级, 编号)]，栈顶是当前父标题
    children: dict[str, int] = {}              # 父编号 → 已出现的子标题个数
    for depth, kind, value in items:
        if kind == "cjk":
            problems.append("%s内嵌小标题用了汉字序号（可写成章节号.序号）" % where)
            continue
        segments = _number_segments(value)
        if segments is None:
            continue
        repeated = value in seen
        seen.add(value)
        if repeated:
            # 重号是正文自身就能证明的问题，不依赖章号基准
            problems.append("%s内编号 %s 重复" % (where, value))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else chapter_number
        if not parent or (not stack and not anchor_reliable):
            # 章号与父标题都无从确定（或章号本身不可靠）：只判层数，不给期望形态
            if len(segments) > MAX_NUMBER_DEPTH:
                problems.append("%s下的编号 %s 共 %d 层（编号最多 %d 层）"
                                % (where, value, len(segments), MAX_NUMBER_DEPTH))
            stack.append((depth, value))
            continue
        if repeated:
            stack.append((depth, value))
            continue
        index = children.get(parent, 0) + 1
        children[parent] = index
        expected = "%s.%d" % (parent, index)
        parent_segments = _number_segments(parent) or []
        if segments[:-1] != parent_segments:
            # 父链对不上优先说：它同时解释了「层数多出来」的由来（父编号之后又拼了一段）
            if parent == chapter_number:
                note = "编号前缀与本章章号 %s 不一致" % chapter_number
            else:
                note = "父编号 %s 后只接一层序号" % parent
            problems.append("%s下的编号 %s 应为 %s（%s）" % (where, value, expected, note))
        elif len(segments) > MAX_NUMBER_DEPTH:
            problems.append("%s下的编号 %s 共 %d 层（编号最多 %d 层，父编号 %s 之下不再分层）"
                            % (where, value, len(segments), MAX_NUMBER_DEPTH, parent))
        elif segments[-1] != index:
            problems.append("%s下的编号 %s 应为 %s（同级编号从 1 起连续）"
                            % (where, value, expected))
        stack.append((depth, value))
    return problems


def _known_numbers(chapters: list[dict[str, Any]], outline: dict[str, str]) -> set[str]:
    """文档里「存在的标题编号」全集。

    = 可靠的章节章号（大纲算出来的、标题里写的、与正文首条编号互证的 seq，
      连同它自带的祖先段）+ 各章正文里出现过的小标题编号。
    父链上每一段都要落在这个集合里，才算这条父链接得上。

    裸 seq 不进这个集合：它是行序、不是标题里的编号，拿它冒充「父编号存在」等于替文档
    编一个章节出来（成稿实测：`4.1.1` 的父编号 `4.1` 就是这么被判错的）。只有上半段
    `_corroborated_number()` 认定的互证章号才算——它与正文首条编号本来就是同一个数。
    """
    known: set[str] = set()
    rows = chapters if isinstance(chapters, (list, tuple)) else []
    numbers: list[str] = []
    for chapter in rows:
        if not isinstance(chapter, dict):
            continue
        for candidate, source in _chapter_number_candidates(chapter, outline):
            if source in RELIABLE_NUMBER_SOURCES:
                numbers.append(candidate)
        corroborated, _source = _corroborated_number(chapter, outline)
        if corroborated:
            numbers.append(corroborated)
    for number in numbers:
        segments = _number_segments(number) or []
        for size in range(1, len(segments) + 1):   # 章号自带的祖先段也算存在
            known.add(_number_text(segments[:size]))
    for chapter in rows:
        if not isinstance(chapter, dict) or _is_quoted(chapter):
            continue
        for _depth, kind, value in _numbered_headings(str(chapter.get("content") or "")):
            if kind != "cjk" and _number_segments(value) is not None:
                known.add(value)
    return known


def _numbering_anchors_complete(chapters: list[dict[str, Any]],
                                outline: dict[str, str]) -> bool:
    """整册每一章是否都能定出可靠章号（决定能不能判「父编号不存在」）。

    只要有一章算不出可靠章号，`_known_numbers()` 就是**不全**的：某个「找不到的父编号」
    很可能只是我们没把那一章的章号算进来（线上语料不带 id 时尤其如此）。这时判「编号
    孤立」等于拿残缺的集合去指认文档，所以一律不判——父链数据不全不是投标人的错误。
    """
    rows = chapters if isinstance(chapters, (list, tuple)) else []
    for chapter in rows:
        if not isinstance(chapter, dict):
            continue
        if _is_quoted(chapter):
            continue
        items = _numbered_headings(str(chapter.get("content") or ""))
        first = items[0][2] if items and items[0][1] != "cjk" else ""
        number, source = _pick_chapter_number(chapter, outline, first)
        if source == NUMBER_SOURCE_SEQ:
            number, source = _corroborated_number(chapter, outline)
        if not number or source not in RELIABLE_NUMBER_SOURCES:
            return False
    return True


def _needs_parent_chain(items: list[tuple[int, str, str]]) -> bool:
    """这一章有没有「父编号」要核对（≥2 段的编号才有父链）。"""
    for _depth, kind, value in items:
        if kind == "cjk":
            continue
        segments = _number_segments(value)
        if segments and len(segments) >= 2:
            return True
    return False


def _orphan_problems(chapter: dict[str, Any], chapter_number: str, number_source: str,
                     known: set[str], reported: set[str],
                     anchors_complete: bool = True) -> list[str]:
    """这一章正文里父链找不到的编号（编号孤立），同一个缺失父编号只报一次。

    成立条件有两个，缺一个都不判：
    ① 章号来源可靠（大纲编号 / 标题自带编号 / 与正文首条编号互证的 seq）。章号只来自
       裸 seq 或干脆拿不到时，我们连「这一章是第几章」都不知道，父链无从核对；
    ② 整册每一章都能定出可靠章号（`anchors_complete`）。否则「找不到的父编号」可能只是
       我们自己没算出来——线上语料不带 id 时就是这样（用户实测：`6.1.1` 的父编号 `6.1`）。
    任一条不满足一律返回空（不判孤立、不猜），由调用方写成「本次未能核对章节编号」。
    """
    if number_source not in RELIABLE_NUMBER_SOURCES or not anchors_complete:
        return []
    if not isinstance(chapter, dict) or _is_quoted(chapter):
        return []
    content = str(chapter.get("content") or "")
    if not content.strip():
        return []
    items = _numbered_headings(content)
    if not items:
        return []
    title = str(chapter.get("title") or "").strip() or "未命名章节"
    where = _chapter_where(title, chapter_number)
    problems: list[str] = []
    for _depth, kind, value in items:
        segments = _number_segments(value)
        if kind == "cjk" or not segments or len(segments) < 2:
            continue
        for size in range(1, len(segments)):
            prefix = _number_text(segments[:size])
            if prefix in known:
                continue
            if prefix not in reported:
                reported.add(prefix)
                problems.append("%s下的编号 %s 的父编号 %s 在文档标题里找不到（编号孤立）"
                                % (where, value, prefix))
            break
    return problems


def _check_c11(chapters: list[dict[str, Any]]) -> dict[str, Any] | None:
    """目录与章节编号规范：按**完整编号路径**判父子、同级连续、层数与父链存在性。

    口径必须与生成侧**同一套**判据（`chapter_rules.SUBHEADING_RE` + `heading_number`）：
    - 只认 `##` 及更深的小标题。`#` 是章节标题本身，编号由导出器按大纲统一写入，
      正文里再冒出一个 `# 1.1 …` 属于排版残留，不算「内嵌编号」；
    - 照录招标格式的章节（投标函/授权委托书/偏差表/报价表…）整章跳过：它们的编号、
      层级是招标人规定的，按作者视角判必然误伤。

    旧实现只比编号第一段（`n.split(".")[0]`），于是 `6.3.1` / `6.3.1.3` / `6.3.1.3.1`
    都以 `6` 开头、必然放行（成稿 v3 里 25 处 `6.3.1.3.1` 就是这么漏掉的）。现在的判据：
    a. 子编号 = 父编号 + 恰好一层（父 `6.3.3` 下写 `6.3.1.3.1` 应改为 `6.3.3.1`）；
    b. 同一父编号下的序号从 1 起连续，不跳号、不重号；
    c. 编号最多 4 层（a.b.c.d）；
    d. 父链上每一段都要能在文档标题里找到，否则报「编号孤立」——**但只有章号来源可靠
       （大纲编号 / 标题自带编号 / 与正文首条编号互证的 seq）且整册章号齐全时才判**。
    合法的多层编号（父 `6.1.3` 下的 `6.1.3.1`）照常放行。

    「编号孤立」与「不可核对」必须分开：拿到章号的前提是章节行带 id（`load_corpus` 已带）
    或标题自带编号，导入的标书两者都没有，seq 又只是行序。此时拿 seq 当章号判编号，就会把
    成稿里本身正确的编号（`6.1.1` 在 6.1 节下、`14.1` 写在 seq=34 的章里）判成
    「父编号找不到 / 应为 34.1」，并同时显示「未能定位章节」——用户实测的误报。缺章号是
    **我们的数据缺口**，不是投标人的编号错误：这种情况整条结论写成「通过 + 本次未能核对
    章节编号」，绝不判「必须修改」。
    """
    outline = _outline_numbers(chapters)
    known = _known_numbers(chapters, outline)
    anchors_complete = _numbering_anchors_complete(chapters, outline)
    reported: set[str] = set()
    problems: list[str] = []
    checked = 0
    unverified: list[dict[str, Any]] = []       # 章号无从核对、因而跳过父链核对的章
    problem_at: tuple[dict[str, Any], str] | None = None   # 第一个出问题的章（定位用）
    first_at: tuple[dict[str, Any], str] | None = None     # 第一个有编号小标题的章
    # 入参可能是 None / 非列表 / 混着脏行：只认 dict，其余跳过，绝不抛异常
    rows = chapters if isinstance(chapters, (list, tuple)) else []
    for chapter in rows:
        if not isinstance(chapter, dict):
            continue
        title = str(chapter.get("title") or "").strip() or "未命名章节"
        content = str(chapter.get("content") or "")
        if not content.strip() or _is_quoted(chapter):
            continue
        items = _numbered_headings(content)
        if not items:
            continue
        checked += 1
        first = items[0][2] if items[0][1] != "cjk" else ""
        chapter_number, number_source = _pick_chapter_number(chapter, outline, first)
        if number_source == NUMBER_SOURCE_SEQ:
            # seq 只有与正文首条「章号.N」互证时才升格（见 `_corroborated_number`）
            number, source = _corroborated_number(chapter, outline)
            if number:
                chapter_number, number_source = number, source
        if first_at is None:
            first_at = (chapter, chapter_number)
        chapter_problems = _numbering_problems(title, chapter_number, items, number_source)
        if _needs_parent_chain(items) and number_source not in RELIABLE_NUMBER_SOURCES:
            # 章号只来自 seq（未互证）或没有章号：父链无从核对，绝不报「父编号找不到」
            unverified.append(chapter)
        else:
            chapter_problems.extend(
                _orphan_problems(chapter, chapter_number, number_source, known, reported,
                                 anchors_complete))
        problems.extend(chapter_problems)
        if chapter_problems and problem_at is None:
            problem_at = (chapter, chapter_number)
    if problems:
        chapter, _number = problem_at or first_at or ({}, "")
        detail = "章节编号存在问题：%s" % "；".join(problems[:3])
        evidence = "；".join(problems[:3])[:200]
        locator = _chapter_locator(chapter)
        if not locator:
            # 指不出位置的结论不能拦人：既说「必须修改」又说不出改哪里，等于让用户自己猜。
            # 章标题为空时降级为待复核（中性），并在文案里说明为什么定位不到。
            return _items("C11", "待复核", "目录 / 章节标题",
                          "%s。本次未能定位到具体章节（该章标题为空），请按上述编号在本册正文中"
                          "自行核对" % detail, evidence, locator_missing=True)
        return _items("C11", "必须修改", _hit_with_chapter(chapter), detail, evidence, **locator)
    if not checked:
        # 正文没有带编号的小标题（例如刚生成、或正文用纯文本小标题）：无从判定，交给上层
        return None
    if not _mostly_written(rows):
        # 只写了一两章就判「编号全对」会把结论说过头
        return None
    if unverified:
        # 不可核对 ≠ 必须修改：给「通过 + 未能核对」，并把命中章节定位填上，界面不再说「未定位」
        return _items("C11", "通过", _hit_with_chapter(unverified[0]),
                      "%s：%d 章正文的小标题编号没有可核对的章号（大纲编号、标题编号均未取到），"
                      "已跳过父链核对，本次不判孤立编号；已核对的编号未发现重号、跳号或层级超限"
                      % (UNVERIFIED_NUMBER_NOTE, len(unverified)),
                      **_chapter_locator(unverified[0]))
    if not anchors_complete:
        # 章号基准齐全但父链集合不完整：能核对的部分照实说，孤立编号不判
        return _items("C11", "通过", "目录 / 章节标题",
                      "%s：本册有章节的章号算不出来（大纲编号、标题编号均未取到），"
                      "父链数据不完整，本次不判孤立编号；已按章检查 %d 章正文的重号、跳号、"
                      "层级超限与父子层数" % (UNVERIFIED_NUMBER_NOTE, checked))
    return _items("C11", "通过", "目录 / 章节标题",
                  "已按章检查 %d 章正文的小标题编号，未发现重号、跳号、层级超限或孤立编号"
                  % checked)


def _check_c06(chapters: list[dict[str, Any]], dims: list[dict[str, Any]]) -> dict[str, Any] | None:
    """项目名称与编号一致性：只核对代码类编号，命中冲突才判必须修改。"""
    codes = [c for c in (_dim_value(dims, "项目编号"), _dim_value(dims, "招标编号")) if c]
    if not codes:
        return None
    body = _body(chapters)
    if not body.strip():
        return None
    found = set(_CODE_RE.findall(body))
    wrong = sorted(found - set(codes))
    if wrong:
        return _items("C06", "必须修改", "正文中的项目编号",
                      "正文出现与招标文件不一致的编号：%s（招标文件为 %s）"
                      % ("、".join(wrong[:4]), "、".join(codes)),
                      "不一致编号：%s" % "、".join(wrong[:4]))
    if any(code in body for code in codes):
        return _items("C06", "通过", "正文中的项目编号",
                      "正文出现的项目编号与招标文件一致（%s）" % "、".join(codes))
    return None


def _check_c22(chapters: list[dict[str, Any]]) -> dict[str, Any] | None:
    """跨章数值口径一致性：同一口径在不同章节写出不同数值时，**只提示核对**。

    为什么需要：实测某项目第 12 节写「履约保证金按合同含税总金额的 10% 提交」，
    第 31 节前附表写「履约保证金：中标含税金额的 2%」—— 同一口径两个值，
    此前 21 项规则里没有任何一项会发现它（C09 商务条款逐条响应只看「有没有写」）。

    三条边界，缺一不可：
      · **只报告、不改写**：不替你选 10% 还是 2%，不替换数值、不删句子；
      · **不进导出阻断**：结论只给「待复核」，永远不给「必须修改」；
      · **定位不到就不报**：没有章标题/章序的候选直接丢弃（见 value_consistency）。
    """
    from zhibiao.core.value_consistency import describe_conflicts, scan_value_conflicts

    if not chapters or not _body(chapters).strip():
        return None
    conflicts = scan_value_conflicts(chapters)
    if not conflicts:
        return _items("C22", "通过", "全篇商务口径",
                      "已逐章比对可核对的商务口径（履约/投标保证金、质保期、交货期、"
                      "质保金比例、付款环节比例、违约金比例、随机备件比例、零整比），"
                      "未发现同一口径出现不同数值")
    first = conflicts[0]
    hit = "跨章数值口径：%s（第 %s 节「%s」）" % (
        first.get("label") or first.get("key"), first.get("chapter_seq"),
        first.get("chapter_title"))
    return _items("C22", "待复核", hit,
                  describe_conflicts(conflicts),
                  str(first.get("quote") or ""),
                  chapter_id=str(first.get("chapter_id") or ""),
                  chapter_title=str(first.get("chapter_title") or ""),
                  chapter_seq=int(first.get("chapter_seq") or 0),
                  conflicts=conflicts)


def _check_exporter_items(cid: str, imported: bool) -> dict[str, Any] | None:
    """页眉页脚 / 字体字号：平台生成的正文由导出器强制写入，导入的标书无法从文本判定。"""
    if imported:
        return None
    if cid == "C12":
        return _items("C12", "通过", "页眉 / 页脚",
                      "导出时按项目名称统一写入页眉页脚（由导出代码强制，不依赖模型）")
    if cid == "C13":
        return _items("C13", "通过", "正文样式",
                      "导出时按设置的字体、字号、行距统一排版（由导出代码强制，不依赖模型）")
    return None


def deterministic_item(cid: str, *, chapters: list[dict[str, Any]], source_text: str = "",
                       dims: list[dict[str, Any]] | None = None,
                       scores: list[dict[str, Any]] | None = None,
                       imported: bool = False, scope: str = "unknown") -> dict[str, Any] | None:
    """算得准就给结论，算不准返回 None（不是「通过」）。"""
    dims = dims or []
    scores = scores or []
    try:
        if cid == "C06":
            return _check_c06(chapters, dims)
        if cid == "C08":
            return _check_c08(chapters, source_text)
        if cid == "C10":
            return _check_c10(chapters, scores, scope)
        if cid == "C11":
            return _check_c11(chapters)
        if cid == "C22":
            return _check_c22(chapters)
        if cid in ("C12", "C13"):
            return _check_exporter_items(cid, imported)
    except Exception:  # noqa: BLE001 - 校验层自身出错不能拖垮整份报告
        return None
    return None