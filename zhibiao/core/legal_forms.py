"""「法定代表人身份证明」「授权委托书」这类按格式填写的法务文书章节。

为什么单独处理：

1. 它们**不是**材料扫描件。营业执照、检测报告这些是第三方出具、只能插入原件；
   身份证明与授权委托书是投标人自己出具、按招标文件格式填写的文书。旧实现把它们
   塞进「材料占位」规则里，整章只输出「本节用于提交法定代表人身份证明扫描件」
   一句话，正文等于没写 —— 用户实测就是这里一直错。
2. 也不能交给模型自由发挥：这类文书里全是自然人姓名、身份证号、公司名、印章位置，
   模型一定会编（编错的姓名/证件号在形式评审里是废标级问题）。
3. 招标文件第六章常常**只列名称不给格式原文**（实测这份文件就是这样：目录里有一行
   「二、法定代表人身份证明」，正文里没有它的格式）。所以两种情况都要能出稿：
   - 招标文件给了格式原文 → 照抄，一字不改，填空处保留原样（导出时转红色待填）；
   - 只给了名称 → 用通用文书格式，人物/公司/证件一律 {{待填:…}}，
     项目名称、招标编号、招标人这些招标文件里有的事实预填进去。

4. 招标文件写明「不接受联合体投标」「不接受代理商投标」时，这两类根本不适用的章节
   （《联合体协议书》整章、《制造商授权书》小节）不能删、也不能假装在填，只能保留
   招标人规定的格式 + 末尾一行加粗说明（`ensure_applicability_note`）。判定条件全部
   来自招标原文：原文允许联合体 / 允许代理投标时，一个字都不加。

铁律：不编造自然人姓名、身份证号、统一社会信用代码、日期。
"""
from __future__ import annotations

import re
from typing import Any

# 代理口径复用 `text_hygiene` 里已有的判定，不另写一套正则：两个模块各写一遍，
# 迟早出现「正文说不是代理商、注里说本项不适用」这种自相矛盾的成稿。
from zhibiao.core.text_hygiene import tender_rejects_agents

# 章节标题 → 文书类型。顺序有意义：「法定代表人身份证明」要先于「授权委托书」之外判断，
# 两者标题互不含糊；「授权委托」是常见简写。
_FORM_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("法定代表人身份证明", ("法定代表人身份证明", "法人身份证明", "法定代表人证明",
                            "单位负责人身份证明")),
    ("授权委托书", ("授权委托书", "授权委托", "法人授权书", "法定代表人授权书")),
)

# 标题里出现这些词说明它要「写正文」而不只是「填格式」，不走本模块
_PROSE_HINTS = ("说明", "流程", "管理", "分析", "汇总", "解读", "方案", "制度", "要点")


def legal_kinds(title: str) -> list[str]:
    """这一章包含哪几种法务文书（按文书在投标文件里的先后顺序）。

    大纲里常见把两件文书合成一章（「法定代表人身份证明与授权委托书」），
    这时必须两份都出，只出第一份等于漏了一件。
    """
    clean = re.sub(r"\s+", "", str(title or ""))
    if not clean:
        return []
    kinds: list[str] = []
    for kind, words in _FORM_KINDS:
        for word in words:
            if word in clean:
                remainder = clean.replace(word, "")
                # 「授权委托书填写说明」这类是要写正文的说明章，不是文书本身
                if not any(hint in remainder for hint in _PROSE_HINTS):
                    kinds.append(kind)
                break
    return kinds


def legal_kind(title: str) -> str:
    """兼容旧调用：返回第一种文书类型；不属于本模块返回空串。"""
    kinds = legal_kinds(title)
    return kinds[0] if kinds else ""


def _dim_value(dims: Any, *names: str) -> str:
    """按**调用方给的优先级**取一个可直接使用的事实值（未识别/残片一律不用）。

    注意不能按 dims 的存储顺序取：项目里同时有「招标编号」和「项目编号」两项时，
    存储顺序常常把项目编号排在前面，取错了就会在授权委托书里写错编号。
    """
    rows = dims or []
    if isinstance(rows, dict):
        rows = [{"name": key, "value": value} for key, value in rows.items()]
    by_name: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "hit" in row and not row.get("hit"):
            continue
        value = str(row.get("value") or "").strip()
        if not value or "未识别" in value or "待填" in value:
            continue
        if len(value) > 60:          # 抽到的是整段定义句，不如留待填
            continue
        by_name.setdefault(str(row.get("name") or ""), []).append(value)
    for name in names:
        for value in by_name.get(name, []):
            return value
    return ""


def _fill(label: str) -> str:
    return "{{待填:%s}}" % label


def _canonical_body(kind: str, dims: Any, tender_named: bool) -> str:
    """招标文件没给格式原文时的通用文书（事实能填的填，人物信息一律待填）。"""
    project = _dim_value(dims, "招标项目名称", "项目名称", "标的名称")
    # 招标编号优先；没有时用项目编号，但标签必须跟着改，不能把项目编号写成「招标编号」
    code = _dim_value(dims, "招标编号", "招标项目编号")
    code_label = "招标编号"
    if not code:
        code = _dim_value(dims, "项目编号")
        code_label = "项目编号"
    tenderee = _dim_value(dims, "招标人", "采购人", "招标单位")
    project_text = project or _fill("招标项目名称")
    code_text = ("（%s：%s）" % (code_label, code)) if code else ""
    tenderee_text = tenderee or _fill("招标人名称")

    if kind == "法定代表人身份证明":
        return (
            "法定代表人身份证明\n\n"
            "投标人名称：%s\n\n"
            "单位性质：%s\n\n"
            "地址：%s\n\n"
            "成立时间：%s\n\n"
            "经营期限：%s\n\n"
            "姓名：%s　性别：%s　职务：%s\n\n"
            "身份证号码：%s\n\n"
            "系 %s 的法定代表人。\n\n"
            "特此证明。\n\n"
            "附：法定代表人身份证复印件（正反面）。\n\n"
            "投标人：%s（加盖单位公章）\n\n"
            "日期：%s\n"
            % (_fill("投标人全称"), _fill("单位性质"), _fill("注册地址"),
               _fill("成立日期"), _fill("营业期限"),
               _fill("法定代表人姓名"), _fill("性别"), _fill("法定代表人职务"),
               _fill("身份证号码"),
               _fill("投标人全称"), _fill("投标人全称"), _fill("年月日"))
        )

    return (
        "授权委托书\n\n"
        "本授权委托书声明：我 %s（姓名）系 %s（投标人名称）的法定代表人，"
        "现授权委托 %s（姓名）为我公司代理人，以本公司名义参加 %s %s 的投标活动、"
        "开标及合同谈判，代理人在上述活动中所签署的一切文件和处理与之有关的一切事务，"
        "我均予以承认。\n\n"
        "代理人无转委托权。特此委托。\n\n"
        "代理人：%s　性别：%s　身份证号：%s\n\n"
        "职务：%s\n\n"
        "投标人：%s（加盖单位公章）\n\n"
        "法定代表人：%s（签字或盖章）\n\n"
        "日期：%s\n\n"
        "附：代理人身份证复印件（正反面）。\n"
        % (_fill("法定代表人姓名"), _fill("投标人全称"), _fill("代理人姓名"),
           project_text, code_text,
           _fill("代理人姓名"), _fill("性别"), _fill("身份证号码"),
           _fill("代理人职务"), _fill("投标人全称"), _fill("法定代表人姓名"),
           _fill("年月日"))
    )


def _single_form_body(kind: str, tender_text: str, dims: Any) -> str:
    """出一份文书的正文：招标文件有格式原文就照抄，否则用通用文书 + 待填。"""
    section = None
    if str(tender_text or "").strip():
        try:
            from zhibiao.core.tender_checkup import format_document_section
            # 用文书类型去查，不能用章节标题：合并标题（「…身份证明与授权委托书」）
            # 会同时命中两份文书，导致授权委托书里抄了身份证明的原文。
            section = format_document_section(tender_text, kind)
        except Exception:  # noqa: BLE001 - 格式抽取失败不能挡住出稿
            section = None

    quoted = str((section or {}).get("body") or "").strip()
    if len(quoted) >= 60:
        # 招标文件里有这份文书的格式原文：照抄，说明写在末尾（正文必须是文书本身，
        # 开头先来一段系统说明，出件时还得手动删）。
        where = ""
        if section and section.get("label"):
            where = "（%s）" % section["label"]
        # 照抄的是 PDF 抽出的纯文本：落款「年 月 日」下面常挂着页脚页码（54、55），
        # 文书末尾还可能有「……」空白占位线。这两行是排版残片、不是文书内容，
        # 在照录处就清掉（实测原样进了成稿，交给评委就是一份带页码残片的文书）。
        from zhibiao.core.text_hygiene import strip_ellipsis_lines, strip_page_numbers_near_signature
        quoted, _ = strip_page_numbers_near_signature(quoted)
        quoted, _ = strip_ellipsis_lines(quoted)
        # 照抄原文时不加任何系统说明：说明放在章节来源里。
        return quoted

    body = _canonical_body(kind, dims, tender_named=bool(section))
    note = ("（填写说明：招标文件「投标文件格式」章节只列了本项名称、未给出格式原文，"
            "以上为通用格式，出件前请与招标文件要求逐项核对；红色待填处必须由人工据实填写"
            "并加盖印章。）")
    return body + "\n\n" + note


def legal_form_body(title: str, tender_text: str = "", dims: Any = None) -> str | None:
    """给这一类章节出稿；不属于本模块返回 None。

    一章里合并了两件文书（「法定代表人身份证明与授权委托书」）时两份都出。
    """
    kinds = legal_kinds(title)
    if not kinds:
        return None
    parts = [_single_form_body(kind, tender_text, dims) for kind in kinds]
    return "\n\n\n".join(part.strip() for part in parts if part.strip())


# ══════════════════════ 不适用说明（联合体 / 制造商授权） ══════════════════════
#
# 招标文件写明「不接受联合体投标」时，《联合体协议书》这一章按定义就不是投标人该填的东西；
# 但**章不能删**：它是招标文件第六章规定的格式之一，目录里少了它，形式评审就是缺项。
# 同理，「不接受代理商投标」时《制造商授权书》小节与本项目无关（投标人就是制造商）。
#
# 处理口径：格式照旧保留，末尾追加一行**加粗**说明，让评委一眼看到「本项不适用」。
# 判定条件全部来自招标原文（代理口径直接复用 `text_hygiene.tender_rejects_agents`），
# 原文**允许**联合体 / 允许代理投标时一个字都不加 —— 允许却写「本项不适用」等于自己
# 声明不符合资格，是比不写更严重的形式评审风险。
#
# 位置：联合体的注放在该章**落款之后**（没有落款就放章末最后一行）；制造商授权书的注放在
# **该小节末尾**（下一小节标题之前）。幂等：正文里已经有这条注就不再加第二次。

# 招标原文的否决口径（两种语序都认，与 `text_hygiene._AGENT_REJECT` 同一套写法）
_CONSORTIUM_REJECT: tuple[re.Pattern[str], ...] = (
    # 不接受联合体 / 不允许联合体投标 / 不接收联合体 / 不予接受联合体 / 拒绝联合体
    re.compile(r"(?:不接受|不允许|不接收|不予接受|拒绝)[^。；\n]{0,12}(?:联合体)"),
    # 联合体不接受 / 联合体不得（参加/组成） / 联合体不予接受
    # 排除紧跟在「联合体」后面的各方义务条款：「联合体各方不得再以自己名义单独投标」
    # 「联合体协议书不得涂改」讲的是成员该怎么做，不是招标人不接受联合体 —— 误判成
    # 否决口径，就会在允许联合体的项目里硬加一行「本项不适用」。
    re.compile(r"(?:联合体)(?!(?:各方|成员|全体成员|牵头人|协议))"
               r"[^。；\n]{0,8}(?:不接受|不得|不予)"),
)

_NOTE_CONSORTIUM = "注：本项目不接受联合体投标，本项不适用，特此说明。"
_NOTE_AGENT = "注：本项目不接受代理商投标，我方为投标设备制造商，本项不适用，特此说明。"

# 《联合体协议书》章：标题含「联合体」，且是协议/声明类文书（不是「联合体投标策略分析」）
_CONSORTIUM_TITLE_HINT = re.compile(r"联合体")
_CONSORTIUM_FORM_HINT = re.compile(r"协议|声明|承诺|书")
# 《制造商授权书》小节：允许「（七）」「7.」「（如有）」等前缀与后缀，中间夹「出具的」也认
_AGENT_TITLE_HINT = re.compile(r"制造商[^。；\n]{0,8}?授权")

# 落款行（短行才算：长句里出现「日期」不代表这是落款）
_SIGN_TAIL = re.compile(
    r"(?:投标人|供应商|联合体各方|联合体成员|联合体牵头人|牵头人|成员单位|法定代表人"
    r"|委托代理人|经办人|签字|签章|盖章|公章|日期|年\s*月\s*日)"
)
_SIGN_LINE_MAX = 40

# 小节标题：严格口径（「（七）」「（一）」或 markdown 标题）用来找这一小节的**结束位置**
_HEADING_STRICT = re.compile(
    r"^(?:#{1,6}\s|[（(]\s*(?:[一二三四五六七八九十]{1,3}|\d{1,2})\s*[）)])"
)
# 宽松口径（多认「7、」「7.」）只用来**找小节开头**：正文里的「1、授权产品：…」这种
# 编号行不能被当成下一个小节标题，否则注会插到小节中间。
_HEADING_LOOSE = re.compile(
    r"^(?:#{1,6}\s|[（(]\s*(?:[一二三四五六七八九十]{1,3}|\d{1,2})\s*[）)]|\d{1,3}\s*[、.．])"
)


def _flat_note_text(text: str) -> str:
    """去掉空白与加粗标记：判「这条注是不是已经有了」不能受 `**` 与换行影响。"""
    return re.sub(r"[\s*]+", "", str(text or ""))


def _bold(note: str) -> str:
    """正文是 Markdown，`**…**` 是**既有**的加粗写法（导出 Word 时由 `_md_add_runs`
    转成加粗 run），这里不新造语法。"""
    return "**%s**" % note


def tender_rejects_consortium(tender_text: str) -> bool:
    """招标原文是否明确「不接受联合体投标」。

    两种语序都认（「不接受联合体」「联合体不得」）；原文**允许**联合体时返回 False。
    只看招标人自己的否决口径，不猜、不按行业惯例推断。
    """
    text = str(tender_text or "")
    if not text.strip():
        return False
    flat = re.sub(r"\s+", "", text)
    return any(pattern.search(flat) for pattern in _CONSORTIUM_REJECT)


def _is_consortium_chapter(clean_title: str) -> bool:
    """这一章是不是《联合体协议书》（「…填写说明」这类要写正文的说明章不算）。"""
    if not _CONSORTIUM_TITLE_HINT.search(clean_title):
        return False
    if not _CONSORTIUM_FORM_HINT.search(clean_title):
        return False
    remainder = _CONSORTIUM_FORM_HINT.sub("", _CONSORTIUM_TITLE_HINT.sub("", clean_title))
    return not any(hint in remainder for hint in _PROSE_HINTS)


def _is_agent_authorization(clean_title: str) -> bool:
    """这一段是不是《制造商授权书》小节（编号与「（如有）」已由调用方剥掉）。"""
    if not _AGENT_TITLE_HINT.search(clean_title):
        return False
    remainder = _AGENT_TITLE_HINT.sub("", clean_title)
    return not any(hint in remainder for hint in _PROSE_HINTS)


def _append_block(body: str, block: str) -> str:
    """把一行说明放到正文最后（空正文时就是这一行）。"""
    text = str(body or "").rstrip()
    return (text + "\n\n" + block) if text else block


def _insert_after_signature(body: str, block: str) -> str:
    """把说明放到**落款之后**：命中最后一行落款行就插在它后面，没有落款就放章末。"""
    lines = str(body or "").splitlines()
    last = None
    for index, line in enumerate(lines):
        text = line.strip()
        if text and len(text) <= _SIGN_LINE_MAX and _SIGN_TAIL.search(text):
            last = index
    if last is None:
        return _append_block(body, block)
    head = "\n".join(lines[:last + 1]).rstrip()
    tail = "\n".join(lines[last + 1:]).strip()
    return head + "\n\n" + block + (("\n\n" + tail) if tail else "")


def _agent_subsection_start(body: str) -> int | None:
    """正文里《制造商授权书》小节标题所在行号（没有返回 None）。"""
    for index, line in enumerate(str(body or "").splitlines()):
        text = line.strip()
        if not text or not _HEADING_LOOSE.match(text):
            continue
        if _is_agent_authorization(_flat_note_text(_HEADING_LOOSE.sub("", text))):
            return index
    return None


def _insert_at_subsection_end(body: str, block: str) -> str:
    """把说明放到《制造商授权书》**小节末尾**（下一小节标题之前；没有下一小节就放章末）。"""
    lines = str(body or "").splitlines()
    start = _agent_subsection_start(body)
    if start is None:
        return _append_block(body, block)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        text = lines[index].strip()
        if text and _HEADING_STRICT.match(text):
            end = index
            break
    head = "\n".join(lines[:end]).rstrip()
    tail = "\n".join(lines[end:]).strip()
    return head + "\n\n" + block + (("\n\n" + tail) if tail else "")


def applicability_note(title: str, tender_text: str = "") -> str:
    """这一章（小节）该不该加「本项不适用」说明：加就返回加粗的整行，不加返回空串。"""
    clean_title = _flat_note_text(title)
    if not clean_title or not str(tender_text or "").strip():
        return ""
    if _is_consortium_chapter(clean_title):
        return _bold(_NOTE_CONSORTIUM) if tender_rejects_consortium(tender_text) else ""
    if _is_agent_authorization(clean_title):
        return _bold(_NOTE_AGENT) if tender_rejects_agents(tender_text) else ""
    return ""


def ensure_applicability_note(body: str, source_text: str = "",
                              title: str = "") -> tuple[str, str]:
    """按招标原文条件，给《联合体协议书》章末 /《制造商授权书》小节末补一行加粗说明。

    返回 (新正文, 处理说明)。处理说明为空表示**没改动**，三种情况都原样返回：
    * 招标原文没写「不接受联合体」/「不接受代理商」→ 一个字都不加；
    * 正文里已经有这条注 → 不再加第二次（幂等，整册后处理可反复跑）；
    * 这一章（小节）不是这两类 → 不碰。

    位置的口径见 `_insert_after_signature`（落款之后）与 `_insert_at_subsection_end`
    （《制造商授权书》小节末尾）。
    """
    text = str(body or "")
    if not text.strip():
        return text, ""
    clean_title = _flat_note_text(title)
    if _is_consortium_chapter(clean_title):
        if not tender_rejects_consortium(source_text):
            return text, ""
        if _flat_note_text(_NOTE_CONSORTIUM) in _flat_note_text(text):
            return text, ""
        return (_insert_after_signature(text, _bold(_NOTE_CONSORTIUM)),
                "章末补一行「本项目不接受联合体投标、本项不适用」说明")
    # 授权书可能是独立一章，也可能只是《资格审查资料》里的一个「（七）」小节：
    # 标题对不上时看正文里有没有这个小节标题，有就补在该小节末尾。
    if _is_agent_authorization(clean_title) or _agent_subsection_start(text) is not None:
        if not tender_rejects_agents(source_text):
            return text, ""
        if _flat_note_text(_NOTE_AGENT) in _flat_note_text(text):
            return text, ""
        return (_insert_at_subsection_end(text, _bold(_NOTE_AGENT)),
                "《制造商授权书》小节末补一行「本项目不接受代理商投标、本项不适用」说明")
    return text, ""
