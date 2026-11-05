"""招标文件格式章的**同级小节一个都不能少**：缺哪个补哪个，补的是框架不是正文。

背景（线上问题，废标/丢分级）
----------------------------
实测《新1_2.pdf》第 67~77 页《八、资格审查资料》是招标人规定的**八项**法定格式：
（一）基本情况表、（二）近年财务状况表、（三）近年完成的同类项目情况表、
（四）正在供货和新承接的项目情况表、（五）近年发生的诉讼及仲裁情况、
（六）信誉要求（三网查询截图）、（七）制造商授权书（如有）、（八）不参与围标串标承诺书。
V7 生成的第 9 章只剩（一）（三）（四）三项，漏掉了四个法定小节 —— 其中（六）信誉要求
还是投标人须知 1.4.1(4) 的直接否决项。也就是说：整章形式评审缺项，且漏的是废标项。

根因（两条，缺一不可）
----------------------
1. `tender_checkup._SECTION_LINE` 只认「一、二、…」形态，**不认「（一）（二）」**。
   于是（一）~（八）不是独立小节，全部被并进「八、资格审查资料」这一个 section 的 body：
   既没有"小节"这个粒度，也没有任何地方能发现少了哪几个。
2. 生成链路优先走「结构化表格直出」（`structured_forms.render_structured_section`）：
   它只输出 pdfplumber 检出网格的表 + 表前后紧邻的几行文字。这份 PDF 的第 68~77 页里
   只有 p68（一）、p70（三）、p71（四）检出了网格，于是**没有表格的四节被整段丢弃**，
   正文里连标题都不剩。补全必须以「原文有哪几个小节」为准，而不是以「检出几张表」为准。

本模块的口径
------------
* **只补原文有的**：小节清单来自 `tender_checkup.format_sub_sections()`（招标原文解析），
  原文只有（一）（二）就只出（一）（二），绝不凭空造出（三）；
* **顺序照原文**：小节按招标文件的编号顺序落位，不按检出顺序；
* **幂等**：已经齐全的正文原样返回（逐字不动），重复执行不会重复生成、也不会重排；
* **不接管正文写作**：补的是"法定要素 + 占位框"，具体内容由投标人按实际填写（{{待填:…}}）；
* **数字从原文取**：财务评分口径里的年份与金额阈值全部从招标原文解析，
  解析不到就写"以招标文件评标办法为准"，**不写死 5000 万/1000 万**；
* **复用既有模块**：信誉查询渠道与图框走 `credit_proofs`，图框文案走 `image_slots`，
  不另写一套站点名单与占位语法。
"""
from __future__ import annotations

import re
from typing import Any

from zhibiao.core.credit_proofs import (OTHER_CHANNEL, credit_channel_rows,
                                    credit_channels_in_source, credit_proof_gaps,
                                    credit_proof_scope, row_slot, row_text)
from zhibiao.core.image_slots import image_placeholder
from zhibiao.core.tender_checkup import (format_catalog, format_chapter, format_sections,
                                     format_sub_sections, section_key,
                                     subsection_heading, subsection_heading_at)

# ── 语义识别：哪些小节有法定要素、各是哪一类 ────────────────────────────────
_FINANCE = re.compile(r"财务|审计报告|营业额|营业收入|净利润|资产总额|净资产")
_LITIGATION = re.compile(r"诉讼|仲裁")
_RIGGING = re.compile(r"围标|串标|串通投标")

# ── 财务口径：年份与金额阈值只从招标原文解析 ────────────────────────────────
_THRESHOLD = r"(?:不低于|不少于|不小于|大于等于|≥|达到|超过|高于)"
_AMOUNT = r"([\d,，]+(?:\.\d+)?)\s*(亿元|万元|万|元)"
_TURNOVER_WORDS = r"(?:营业额|营业收入|营业总收入|销售收入)"
_PROFIT_WORDS = r"净利润"
# 年度区间：`2022~2024年`、`2022年1月1日至2024年12月31日`、`（2022-2024）` 都是同一种口径
_YEAR_RANGE = re.compile(
    r"(20\d{2})\s*年?(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?\s*[~～\-—－至到]\s*(20\d{2})\s*年")
# 并列写法：`2022、2023、2024年`
_YEAR_LIST = re.compile(r"(?:20\d{2}\s*年?\s*[、,，和及]\s*){2,}20\d{2}\s*年?")
# 财务口径只在这些句子里找：别的句子里的年份（业绩期间、投标有效期、工期）不能当财务年度
_FINANCE_HINT = re.compile(r"营业额|营业收入|净利润|财务|审计报告")
# 「履约能力 6 分」：评分项名称与分值也来自原文，解析不到就不写分值
_SCORE_ITEM = re.compile(r"履约能力[^。；\n]{0,12}?(\d+(?:\.\d+)?)\s*分")

# 财务状况表的行标签：优先取原文列名，原文没给可用列名时才用通用口径。
_FIN_ROW_HINTS = ("营业额", "营业收入", "销售", "利润", "资产", "负债", "权益", "现金流",
                  "资本", "授信", "信用等级", "存款", "贷款", "净值")
_FIN_DEFAULT_ROWS = ("营业额", "净利润", "资产总额", "负债总额")

# ── 承诺/声明类模板（保留填空位与签章落款位）────────────────────────────────
_SIGN_BLOCK = ("投标人：{{待填:投标人全称}}（加盖单位公章）\n\n"
               "法定代表人（单位负责人）或其委托代理人：{{待填:签字或签章}}\n\n"
               "日期：{{待填:年月日}}")
_SIGNATURE = re.compile(r"(?:公章|盖章|盖单位章|签章|签字)")

_LITIGATION_TEMPLATE = (
    "我单位就近年发生的诉讼及仲裁情况声明如下：\n\n"
    "1. 近三年（{{待填:起止年月}}）我单位{{待填:有/无}}因违约或不恰当履约引起的"
    "合同中止、纠纷、争议、仲裁和诉讼记录。\n\n"
    "2. 如存在上述诉讼及仲裁事项，其事项、涉案金额、审理或裁决结果及对履约能力的"
    "影响逐项说明如下：{{待填:诉讼及仲裁事项逐项说明}}\n\n"
    "3. 我方承诺以上情况真实、完整，无隐瞒或虚假记载。\n\n" + _SIGN_BLOCK
)

_RIGGING_TEMPLATE = (
    "致：{{待填:招标人名称}}\n\n"
    "我单位参加{{待填:招标项目名称}}（{{待填:招标编号}}）投标，现郑重承诺：\n\n"
    "1. 不与其他投标人相互串通投标，不围标、不串标，不与其他投标人约定抬高或压低投标报价；\n\n"
    "2. 不与其他投标人约定中标人，不约定部分投标人放弃投标或中标；\n\n"
    "3. 不与招标人、招标代理机构或评标委员会成员串通，不向其行贿或提供其他不正当利益；\n\n"
    "4. 不以他人名义投标，不借用、挂靠他人资质，不以其他方式弄虚作假骗取中标；\n\n"
    "5. 如违反上述承诺，我单位自愿接受取消投标资格、没收投标保证金等处理，"
    "并依法承担相应责任。\n\n" + _SIGN_BLOCK
)

# 只给了小节标题、正文没抽到时的兜底：保留框架 + 填空位，不写任何"系统说明"。
# 「资质 / 许可 / 账户」三类必须算进来：资格审查里「有效的设备修理资质或制造资质」
# 「基本账户开户许可证」这类小节，正文只有一句话、没有图框，等于没有可交的材料页。
_ATTACH = re.compile(r"授权书|授权委托|证明|证书|执照|报告|凭证|合同|截图|发票|函|资质|许可|账户|备案")
# 表下注释行：「注：…」/「说明：…」/编号注释「2、…」
_NOTE_PREFIX = re.compile(r"^(?:注[：:\s\d]|说\s*明[：:\s]|\d{1,2}\s*[、.．])")


# ── 资格类材料：标题点到就必须有图框（只有文字 = 资格审查缺材料页）─────────────
# (标题命中, 图名, 口径)。口径里必须写清**盖章 / 四角完整 / 有效期**三件事：
# 出件人照着这三句就知道该扫哪一页、要不要盖章、过期了要不要重新办。
_QUALIFICATION_SLOTS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"修理资质|维修资质|检修资质|制造资质|生产资质|设备修理"),
     "设备修理资质或制造资质证书",
     "加盖投标人公章；整页四角完整，须在有效期内（含证书编号、发证机关与许可范围）"),
    (re.compile(r"开户许可证|开户许可|基本存款账户|基本账户|银行开户"),
     "开户许可证或基本存款账户信息",
     "加盖投标人公章；整页四角完整，须在有效期内（开户行名称、账号与银行业务章清晰可辨）"),
)


def qualification_blocks(title: str) -> list[str]:
    """标题点到的资格类材料，各给一段「图名 + 口径 + 图框」。"""
    blocks: list[str] = []
    for pattern, name, requirement in _QUALIFICATION_SLOTS:
        if not pattern.search(str(title or "")):
            continue
        blocks.append("%s（%s）：" % (name, requirement))
        blocks.append(image_placeholder(name))
    return blocks


def qualification_missing(title: str, body: str) -> list[str]:
    """资格类小节里还缺哪些图框（按标题点到的材料逐个核对，幂等）。"""
    text = str(body or "")
    missing: list[str] = []
    for pattern, name, requirement in _QUALIFICATION_SLOTS:
        if not pattern.search(str(title or "")):
            continue
        if name in text:
            continue
        missing.append("%s（%s）：" % (name, requirement))
        missing.append(image_placeholder(name))
    return missing


def _flat(text: Any) -> str:
    return re.sub(r"[ \t\u3000]+", "", str(text or ""))


def _threshold(source_text: str, label: str) -> tuple[str, str]:
    """从招标原文取某项的金额阈值：返回 (原文口径的项目名, 金额)。

    例：「2022~2024 年营业额合计不低于 5000 万元」→ ("营业额合计", "5000万元")。
    项目名带上原文里的「合计/累计」等限定词，成稿的口径才与招标文件一致
    （把「累计」写成「平均」就是两个完全不同的门槛）。
    """
    pattern = re.compile("(" + label + r")(?P<mid>[^。；\n]{0,24}?)" + _THRESHOLD
                         + r"\s*(?:人民币)?\s*" + _AMOUNT)
    found = pattern.search(_flat(source_text))
    if not found:
        return "", ""
    phrase = (found.group(1) + found.group("mid")).strip()
    # 金额在倒数第二个分组前是数字、最后一个分组是单位
    amount = found.group(found.lastindex - 1) + found.group(found.lastindex)
    return phrase, amount.replace(",", "").replace("，", "")


def _years_in(text: str) -> list[int]:
    """一段文字里的年度区间/年度并列清单（解析不到返回空列表）。"""
    flat = _flat(text)
    found = _YEAR_RANGE.search(flat)
    if found:
        first, last = int(found.group(1)), int(found.group(2))
        if 2000 <= first < last <= 2100 and 2 <= last - first + 1 <= 6:
            return list(range(first, last + 1))
    listed = _YEAR_LIST.search(flat)
    if listed:
        years = sorted({int(year) for year in re.findall(r"20\d{2}", listed.group(0))})
        if len(years) >= 2 and years[-1] - years[0] + 1 <= 6:
            return years
    return []


def finance_years(source_text: str) -> list[int]:
    """招标原文规定的财务年度（「2022~2024 年」→ [2022, 2023, 2024]）。

    先在**讲财务/营业额/净利润/审计报告的句子**里找，找不到才退回全文：
    全文里随便出现的年份（投标截止日、工期、业绩期间）不能当财务年度，
    否则占位框数量会跟着别的章节变。解析不到返回空列表（绝不写死年份）。
    """
    parts = re.split(r"[。；\n]", str(source_text or ""))
    for part in [part for part in parts if _FINANCE_HINT.search(part)] + [str(source_text or "")]:
        years = _years_in(part)
        if years:
            return years
    return []


def _score_value(source_text: str) -> str:
    found = _SCORE_ITEM.search(_flat(source_text))
    return found.group(1) if found else ""


def _finance_purpose(source_text: str, years: list[int]) -> str:
    """小节开头的用途说明：评分口径 + 材料口径（数字全部来自原文）。"""
    score = _score_value(source_text)
    head = "本节为资格审查与商务评分「履约能力」项的证明材料"
    if score:
        head += "（该评分项 %s 分）" % score
    head += "："
    period = ("%d~%d 年度" % (years[0], years[-1])) if years else "近三年"
    wants: list[str] = []
    for position, label in enumerate((_TURNOVER_WORDS, _PROFIT_WORDS)):
        phrase, amount = _threshold(source_text, label)
        if not phrase or not amount:
            continue
        # 年度口径只在第一条前面写一次，后面沿用（写两遍读起来像两个不同的期间）
        wants.append(("%s%s不低于%s" % (period, phrase, amount)) if not wants
                     else ("%s不低于%s" % (phrase, amount)))
    if wants:
        head += "、".join(wants) + "，"
    else:
        head += "具体金额与年度口径以招标文件评标办法为准，"
    return head + "以经审计的财务报表（审计报告）为准；未附审计报告的，本项按 0 分计。"


def _finance_row_labels(source_body: str) -> list[str]:
    """财务状况表的行标签：优先用招标原文里给出的项目名，取不到才用通用口径。

    原文常被 PDF 拉平成一行（「近三年营业额 净利润 资产总额 负债总额」），
    所以按空白切成 token 逐个判：短、不带数字、命中财务词才当列名，
    表下注释里的句子（带数字/句读）不会被误当成行标签。
    """
    labels: list[str] = []
    for raw in str(source_body or "").splitlines():
        for token in re.split(r"[ \t\u3000]+", raw.strip()):
            line = token.strip(" 　。，,；;：:")
            if not line or len(line) > 16:
                continue
            # 表格列名已经写了年度，「近三年营业额」这种前缀会拼成
            # 「2022年度近三年营业额」这种重复口径，去掉后再当行标签
            line = re.sub(r"^(?:近[一二三四五六七八九十\d]{1,3}年)", "", line).strip()
            if not line:
                continue
            if re.search(r"\d", line) or re.search(r"[。；;，,：:]", line):
                continue
            if line.startswith(("注", "说明")):
                continue
            if not any(hint in line for hint in _FIN_ROW_HINTS):
                continue
            if line not in labels:
                labels.append(line)
    return labels or list(_FIN_DEFAULT_ROWS)


def _finance_table(source_body: str, years: list[int]) -> str:
    headers = ["序号", "项目"] + (["%d年度" % year for year in years] or ["金额（万元）"])
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for index, label in enumerate(_finance_row_labels(source_body), start=1):
        cells = [str(index), label]
        if years:
            cells.extend("{{待填:%d年度%s}}" % (year, label) for year in years)
        else:
            cells.append("{{待填:%s}}" % label)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _audit_slots(years: list[int]) -> list[str]:
    """审计报告关键页的图片占位框：一年一个，年份来自招标原文。"""
    if not years:
        return [image_placeholder("近三年经审计的财务报表关键页")]
    blocks: list[str] = []
    for year in years:
        kind = "%d年度审计报告关键页" % year
        blocks.append("%s：" % kind)
        blocks.append(image_placeholder(kind))
    return blocks


def _source_notes(source_body: str) -> list[str]:
    """原文里的表下注释（「注：…」「说明：…」「2、…」），照抄到表格下方。

    财务状况表这类格式的注释写的是"本表后附什么材料"（例如按投标人须知 3.5.2 项
    提供经审计的财务报表），是招标人的硬要求，补全时不能丢。
    """
    notes: list[str] = []
    for raw in str(source_body or "").splitlines():
        line = raw.strip(" 　\t")
        if len(line) < 8 or re.fullmatch(r"\d{1,3}", line):
            continue
        if _NOTE_PREFIX.match(line) and line not in notes:
            notes.append(line)
    return notes


def _note_only(text: str) -> bool:
    """整段只有注释行：那不是文书正文，不能当正文用。"""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return bool(lines) and all(_NOTE_PREFIX.match(line) for line in lines)


def _finance_frame(sub: dict[str, Any], tender_text: str) -> str:
    years = finance_years(tender_text)
    parts = [_finance_purpose(tender_text, years),
             _finance_table(str(sub.get("body") or ""), years)]
    parts.extend(_source_notes(str(sub.get("body") or "")))
    parts.extend(_audit_slots(years))
    return "\n\n".join(part for part in parts if part.strip())


def _credit_frame(tender_text: str) -> str:
    """信誉要求：逐渠道一行 + 每行一个图框（渠道与图框全部来自 credit_proofs）。"""
    named = credit_channels_in_source(tender_text)
    rows = credit_channel_rows(tender_text) or named or [OTHER_CHANNEL]
    blocks = ["按招标文件信誉要求逐渠道查询，每个渠道提供一张查询结果截图"
              "（截图须截全网址、查询日期与查询结果，并加盖投标人公章）："]
    for index, channel in enumerate(rows, start=1):
        blocks.append(row_text(index, channel, tender_text))
        blocks.append(row_slot(channel))
    return "\n".join(blocks)


def _source_render(sub: dict[str, Any]) -> str:
    """把这一小节的招标原文渲染成正文（表格按原列还原）；拿不到就照录原文行。"""
    body = str(sub.get("body") or "").strip()
    if not body:
        return ""
    try:
        from zhibiao.core.format_forms import render_section_body
        rendered = str(render_section_body({"title": sub.get("title"), "body": body}) or "").strip()
        if rendered:
            return rendered
    except Exception:  # noqa: BLE001 - 渲染失败不能挡住补全，退回照录
        pass
    # 原文太短（render_section_body 的 _MIN_BODY=60）时它会返回空串：
    # 这时直接照录原文行（去掉页码与"……"占位线），保留原文永远比补模板更接近招标文件。
    kept = [line.strip(" 　\t") for line in body.splitlines()]
    kept = [line for line in kept
            if line and not re.fullmatch(r"\d{1,3}", line) and not re.fullmatch(r"[.…\s]+", line)]
    return "\n".join(kept).strip()


def _commitment_frame(sub: dict[str, Any], template: str, needle: re.Pattern[str]) -> str:
    """承诺/声明类小节：原文有正文就照录（缺签章落款就补上），没有才用模板骨架。

    只有注释行（「注：…填写本表」）不算正文：那是招标人的填表说明，不是承诺内容。
    """
    quoted = _source_render(sub)
    if (quoted and needle.search(quoted) and not _note_only(quoted)
            and len(re.sub(r"\s", "", quoted)) >= 20):
        return quoted if _SIGNATURE.search(quoted) else quoted + "\n\n" + _SIGN_BLOCK
    return template


def _generic_frame(title: str) -> str:
    """其它小节：只给框架与填空位，不替招标人编内容。"""
    lines = ["本节按招标文件规定的格式编制，栏目与签章落款不得改动。",
             "{{待填:%s}}" % title]
    qualified = qualification_blocks(title)
    if qualified:
        lines.extend(qualified)
    elif _ATTACH.search(title):
        lines.append(image_placeholder(title))
    return "\n\n".join(lines)


def _frame_body(sub: dict[str, Any], tender_text: str) -> str:
    """某一小节缺失时应补的正文（不含小节标题行）。"""
    title = str(sub.get("title") or "")
    body = _base_frame_body(sub, tender_text)
    # 资格类材料（资质 / 开户许可）必须带图框：只留一句话等于让投标人自己猜交什么。
    # 放在最后统一补，任何一条出稿路径（照录原文 / 模板 / 通用框架）都不会漏。
    missing = qualification_missing(title, body)
    if missing:
        body = (body.rstrip() + "\n\n" + "\n".join(missing)).strip()
    return body


def _base_frame_body(sub: dict[str, Any], tender_text: str) -> str:
    title = str(sub.get("title") or "")
    if _FINANCE.search(title):
        return _finance_frame(sub, tender_text)
    if _RIGGING.search(title):
        return _commitment_frame(sub, _RIGGING_TEMPLATE, _RIGGING)
    if _LITIGATION.search(title):
        return _commitment_frame(sub, _LITIGATION_TEMPLATE, _LITIGATION)
    if credit_proof_scope(title):
        return _credit_frame(tender_text)
    quoted = _source_render(sub)
    if len(re.sub(r"\s", "", quoted)) >= 20:
        return quoted
    return _generic_frame(title)


# ══════════════════════════════════════════════════════════════════════════
# 正文里的二级小节：识别 → 差集 → 原位补齐
# ══════════════════════════════════════════════════════════════════════════

def _split_subsections(body: str) -> list[tuple[int | None, list[str]]]:
    """把正文按「（一）xxx」标题行切段：[(序号|None, [行…])]，第 0 段是小节前的引言。

    **编号与标题被拆成两行**时（「（二）」一行、渠道名一行，见 `subsection_heading_at`），
    两行合起来才算一个标题：切出来的段首是合并后的一行标题，正文从标题之后开始。
    不合并的话，「（二）」会被当成上一节的正文，补全时又会把（二）整节追加一遍 ——
    同一节出现两次，渠道行与图框也就对不上了。
    """
    lines = str(body or "").splitlines()
    pieces: list[tuple[int | None, list[str]]] = [(None, [])]
    index = 0
    while index < len(lines):
        found, body_start = subsection_heading_at(lines, index)
        if found:
            pieces.append((int(found["ordinal"]), [str(found["heading"]).strip()]))
            index = max(body_start, index + 1)
            continue
        pieces[-1][1].append(lines[index])
        index += 1
    return pieces


def _same_title(left: str, right: str) -> bool:
    """小节标题比对：骨架全等，或一方包含另一方（取号/括号说明的差异容忍掉）。"""
    a, b = section_key(left), section_key(right)
    if not a or not b:
        return False
    return a == b or ((a in b or b in a) and min(len(a), len(b)) >= 3)


def _has_content(lines: list[str]) -> bool:
    """这一段除标题行以外有没有内容（只有标题 = 空表小节，要补框架）。"""
    return any(str(line).strip() for line in lines[1:])


# ══════════════════════════════════════════════════════════════════════════
# 格式章第一节：响应函 / 投标函 / 报价函 正文 + 附录（报价明细）表
# ══════════════════════════════════════════════════════════════════════════
# 背景（废标级，线上实测）：询比采购文件的第六章第一项是「一、响应函」+
# 「响应函附录（报价明细）」，生成结果第 1 章直接从「授权委托书」开始 —— 响应函整项没有，
# 那几条承诺一条都没写；而评审价就是按响应函里的「响应总报价（大写含税）」算的。
# 所以这一节必须**无论叫什么名字都要产出**，且正文里的两个变量（响应总报价大写含税、
# 下浮率）一个都不能少；第一项同时带附录/报价明细时，正文与附录表两样都要有。
# 条数一律按招标原文解析，不写死。
_LETTER_TITLE = re.compile(r"响应函|投标函|报价函|响应书|投标书|报价书")
# 这些是"另一件文书"，不是响应函正文本身
_LETTER_NOT = re.compile(r"承诺书|授权书|委托书|声明|偏差|附录|报价明细|报价表|开标一览")
_APPENDIX_TITLE = re.compile(r"附录|报价明细|分项报价|报价表|开标一览")
_CN_HEAD = re.compile(r"^[ \t]*[一二三四五六七八九十]{1,3}[ \t]*[、.．]")
# 原文里的条款编号：`1．` `1.` `1、` `（1）` `一、` `（一）`
_CLAUSE = re.compile(
    r"^[ \t]*(?:[（(][ \t]*(\d{1,2})[ \t]*[）)]|([一二三四五六七八九十]{1,2})[ \t]*[、.．]"
    r"|(\d{1,2})[ \t]*[、.．)）])[ \t]*(.+)$")
_SENTENCE_TAIL = re.compile(r"[。；;]$")
# 报价明细表的列：# 只认招标原文里真出现过的列名
_QUOTE_COLUMNS = ("序号", "名称", "型号", "规格", "数量", "单位", "限价", "单价", "合价",
                  "总价", "备注", "税率", "交货期", "品牌")
_MONEY_COLS = ("限价", "控制价", "最高", "单价", "合价", "总价", "金额", "小计")
_NUMBER_CELL = re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")


def _title_head(title: str) -> str:
    """标题本体（去掉括注）：「投标函（含投标函附录、报价明细表）」→「投标函」。

    括注里常写着这一项**还带附录**（原文就是这样写的），但那一项仍然是函件正文：
    拿整条标题去比「附录/报价明细」会把第一节整个判成附录、连条款都不解析。
    """
    return re.split(r"[（(]", re.sub(r"\s+", "", str(title or "")), maxsplit=1)[0]


def response_letter_scope(title: str) -> bool:
    """这一节是不是格式章第一项那种「响应函/投标函/报价函」正文。"""
    from zhibiao.core.bid_integrity import requires_authored_content
    if requires_authored_content(title):
        return False
    clean = _title_head(title)
    if not clean or not _LETTER_TITLE.search(clean):
        return False
    return not _LETTER_NOT.search(clean)


def appendix_scope(title: str) -> bool:
    """这一节是不是「附录 / 报价明细 / 报价表」那一块（看标题本体，不看括注）。"""
    clean = _title_head(title)
    return bool(_APPENDIX_TITLE.search(clean))


def _letter_section(tender_text: str) -> dict[str, Any] | None:
    """招标原文里的响应函正文那一节（附录那一块不算），取正文最长的一条。"""
    best: dict[str, Any] | None = None
    for section in format_sections(tender_text):
        title = _title_head(section.get("title") or "")
        if not _LETTER_TITLE.search(title) or _LETTER_NOT.search(title):
            continue
        if best is None or len(str(section.get("body") or "")) > len(str(best.get("body") or "")):
            best = section
    return best


# 编号形态：`1.` / `（1）` / `一、` 各算一组。一级条款只认**最外层**那一组 ——
# 真实事故（《新3.pdf》第 36 页响应函）：原文 7 条一级承诺（`1.`~`7.`），
# 第 2 条下面嵌了 `（1）`~`（6）` 六个子项、第 5 条下面嵌了三个；
# 见行取条时子项也各算一条，成稿写出「以上承诺共 13 条」，编号与原文对不上。
_CN_ORDINAL = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_number(token: str) -> int | None:
    """中文序号转数字（一~十 / 十一~十九 / 二十~九十）；认不出返回 None。"""
    token = str(token or "")
    if token in _CN_ORDINAL:
        return _CN_ORDINAL[token]
    if len(token) == 2:
        head, tail = token[0], token[1]
        if head == "十" and tail in _CN_ORDINAL:
            return 10 + _CN_ORDINAL[tail]
        if tail == "十" and head in _CN_ORDINAL:
            return _CN_ORDINAL[head] * 10
    return None


def _clause_ordinal(found: re.Match[str]) -> tuple[str, int] | None:
    """一条编号行的 (编号形态, 序号)；认不出返回 None。"""
    paren, cn, digit = found.group(1), found.group(2), found.group(3)
    if paren:
        return "paren", int(paren)
    if digit:
        return "digit", int(digit)
    number = _cn_number(cn)
    if number is None:
        return None
    return "cn", number


def _indent_width(line: str) -> int:
    """行首缩进宽度（全角空格算两格，制表符四格）：子项通常比父条款更深一层。"""
    width = 0
    for char in str(line or ""):
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4
        elif char == "\u3000":
            width += 2
        else:
            break
    return width


def _clause_records(body: str) -> list[dict[str, Any]]:
    """正文里所有编号行的「形态 / 序号 / 缩进 / 原文行」（按出现顺序）。"""
    records: list[dict[str, Any]] = []
    for index, raw in enumerate(str(body or "").splitlines()):
        line = raw.strip(" 　\t")
        found = _CLAUSE.match(line)
        if not found:
            continue
        text = found.group(4).strip(" 　\t")
        ordinal = _clause_ordinal(found)
        if not text or ordinal is None:
            continue
        records.append({"form": ordinal[0], "number": ordinal[1], "text": text,
                        "line": line, "indent": _indent_width(raw), "index": index})
    return records


def _consecutive_from_one(records: list[dict[str, Any]]) -> bool:
    """这一组的编号是不是 1、2、3… 连续（一级条款的必要条件）。"""
    return bool(records) and [record["number"] for record in records] == list(
        range(1, len(records) + 1))


def _nests_under(records: list[dict[str, Any]], group: list[dict[str, Any]]) -> bool:
    """别组的编号是否都落在候选组的某一项下面（每个父条款下都从 1 重新连续）。

    真实原文里子项编号在每个父条款下**重新从 1 开始**（第 2 条下 1~6、第 5 条下 1~3），
    所以「形态不同 + 在某条之后重新从 1 起」就是子项；反过来，若候选组前面还有编号行，
    或某一形态在某个父条款下不是从 1 连续下来，候选组就不是最外层。
    """
    members = {id(record) for record in group}
    buckets: dict[int, list[dict[str, Any]]] = {}
    slot = -1
    for record in records:
        if id(record) in members:
            slot += 1
            continue
        if slot < 0:
            return False
        buckets.setdefault(slot, []).append(record)
    for bucket in buckets.values():
        for form in {record["form"] for record in bucket}:
            numbers = [record["number"] for record in bucket if record["form"] == form]
            if numbers != list(range(1, len(numbers) + 1)):
                return False
    return True


def _outermost_group(records: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """最外层那一组编号：从 1 连续、别组都嵌在它下面、条数最多。

    拿不到可靠分组（例如编号从 2 开始、形态混着写）返回 None，由调用方退回逐行取。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(record["form"], []).append(record)
    candidates = [group for group in groups.values() if _consecutive_from_one(group)]
    if not candidates:
        return None
    nested = [group for group in candidates if _nests_under(records, group)]
    # 条数最多者优先；同样多时取缩进更浅（更外层）、出现更早的那一组
    return max(nested or candidates,
               key=lambda group: (len(group), -group[0]["indent"], -group[0]["index"]))


def _flat_letter_clauses(body: str) -> list[str]:
    """逐行取条（旧口径）：所有编号行都算一条，不管是不是子项。"""
    clauses: list[str] = []
    for raw in str(body or "").splitlines():
        line = raw.strip(" 　\t")
        found = _CLAUSE.match(line)
        if not found:
            continue
        text = found.group(4).strip(" 　\t")
        if len(_flat(text)) < 6:
            continue
        if text not in clauses:
            clauses.append(text)
    return clauses


def _sentence_letter_clauses(body: str) -> list[str]:
    """整段话没有编号时的兜底：按句子取（不补造条数）。"""
    clauses: list[str] = []
    for raw in str(body or "").splitlines():
        line = raw.strip(" 　\t")
        if len(_flat(line)) < 10 or not _SENTENCE_TAIL.search(line):
            continue
        if line not in clauses:
            clauses.append(line)
    return clauses[:12]


def _grouped_letter_clauses(body: str) -> list[str] | None:
    """按编号形态分层：只出一级条款，子项拼回父条款正文（拿不到分组返回 None）。

    子项只并入正文、不新增条目，所以成稿的条数与原文一级条款逐条对应；
    拼回去时保留原文行（含子项自己的 `（1）` 编号），评审对着原文能一条条找得到。
    """
    records = _clause_records(body)
    top = _outermost_group(records)
    if not top:
        return None
    members = {id(record) for record in top}
    children: dict[int, list[dict[str, Any]]] = {}
    slot = -1
    for record in records:
        if id(record) in members:
            slot += 1
            continue
        if slot >= 0:
            children.setdefault(slot, []).append(record)
    clauses: list[str] = []
    for position, record in enumerate(top):
        text = record["text"]
        if len(_flat(text)) < 6:
            continue
        merged = "\n".join([text] + [child["line"] for child in children.get(position, [])])
        if merged not in clauses:
            clauses.append(merged)
    return clauses or None


def letter_clauses(tender_text: str) -> list[str]:
    """响应函里的承诺条款本体（条数按原文**一级条款**，解析不到返回空列表）。

    一级条款只认「与全文最外层编号形态一致、且编号从 1 连续」的那一组；子项（缩进更深 /
    编号形态不同 / 父条款还没结束）并进父条款正文，不单独成条 —— 真实事故里 7 条一级
    承诺被数成 13 条，就是子项被当成了一级条款。

    原文没有编号（整段话）时退回按句子取；拿不到可靠分组时退回逐行取（不因此返回空）；
    解析不到才返回空列表，由调用方写一个占位而不是编 7 条出来。
    """
    body = str((_letter_section(tender_text) or {}).get("body") or "")
    if not body.strip():
        return []
    clauses = _grouped_letter_clauses(body)
    if clauses is None:
        clauses = _flat_letter_clauses(body)
    if clauses:
        return clauses
    return _sentence_letter_clauses(body)


def response_letter_body(tender_text: str) -> str:
    """响应函正文骨架：原文承诺条款逐条 + 两个必填变量 + 签章落款。"""
    clauses = letter_clauses(tender_text)
    blocks = ["致：{{待填:采购人（招标人）名称}}",
              "我方已仔细研究{{待填:项目名称}}（{{待填:采购编号}}）采购文件的全部内容，"
              "愿意按本文件承诺的报价承担本项目，现就本项目作出如下响应与承诺："]
    if clauses:
        blocks.extend("%d. %s" % (index, clause)
                      for index, clause in enumerate(clauses, start=1))
        blocks.append("（以上承诺共 %d 条，与招标文件响应函原文逐条对应，不增不减）"
                      % len(clauses))
    else:
        blocks.append("{{待填:按招标文件响应函格式逐条列明承诺事项}}")
    # 评审价按「响应总报价（大写含税）」计算（招标文件评审办法原文），
    # 下浮率是询比采购的固定响应项 —— 两个变量必须同时出现，标签按原文用词。
    for label in price_variables(tender_text):
        blocks.append("%s：{{待填:%s}}" % (label, label))
    blocks.append(_SIGN_BLOCK)
    return "\n\n".join(blocks)


def _split_cells(line: str) -> list[str]:
    text = str(line or "").strip()
    if not text:
        return []
    if "|" in text:
        return [cell.strip() for cell in text.strip("|").split("|") if cell.strip()]
    return [cell for cell in re.split(r"[ \t\u3000]+", text) if cell]


def _appendix_lines(tender_text: str) -> tuple[str, list[str]]:
    """招标原文里附录（报价明细）那一段：(标题, 行)；找不到返回 ("", [])。"""
    span = format_chapter(tender_text)
    lines = span["body"].splitlines() if span is not None else str(tender_text or "").splitlines()
    start = -1
    for index, raw in enumerate(lines):
        text = raw.strip(" 　\t")
        if not text or len(text) > 60:
            continue
        if not _APPENDIX_TITLE.search(_flat(text)):
            continue
        # 正文里顺带提到「报价明细」的句子、以及表下注释都不是标题
        if re.search(r"[。；;]", text) or text.startswith(("注", "说明")):
            continue
        start = index
        break
    if start < 0:
        return "", []
    heading = re.sub(r"^[ \t]*[一二三四五六七八九十]{1,3}[ \t]*[、.．][ \t]*", "",
                     lines[start].strip(" 　\t")).strip() or "附录（报价明细）"
    block: list[str] = []
    for raw in lines[start + 1:start + 60]:
        text = raw.strip(" 　\t")
        if not text:
            continue
        if _CN_HEAD.match(text) or subsection_heading(text):
            break
        block.append(text)
    return heading, block


def _quote_columns(line: str) -> list[str]:
    """报价明细的表头列名（认不出返回空列表）。"""
    cells = _split_cells(line)
    if len(cells) < 3:
        return []
    hits = sum(1 for cell in cells if any(word in cell for word in _QUOTE_COLUMNS))
    return cells if hits >= 3 else []


def _is_money(col: str) -> bool:
    return any(word in col for word in _MONEY_COLS)


def _limit_col(col: str) -> bool:
    return ("限价" in col) or ("控制价" in col) or ("最高" in col)


def _quote_row(cells: list[str], columns: list[str], row_no: int) -> list[str]:
    """一行报价明细：原文有的照抄（限价必须来自原文），没有的空着或待填，绝不编数。"""
    out: list[str] = []
    for index, col in enumerate(columns):
        value = cells[index] if index < len(cells) else ""
        if index == 0 and not value:
            out.append(str(row_no))
            continue
        if not value:
            # 限价解析不到就留空（编一个数出来会直接导致报价越限或越算越低）
            out.append("" if _limit_col(col) else "{{待填}}")
            continue
        if _is_money(col) and not _NUMBER_CELL.match(value.replace("，", ",")):
            out.append("" if _limit_col(col) else "{{待填}}")
            continue
        out.append(value)
    return out


def quotation_appendix(tender_text: str) -> tuple[str, str]:
    """附录（报价明细）表：返回 (标题, markdown 表)；原文没有附录时是 ("", "")。

    列名按原文列名建，行按原文的顺序与条数建；**限价数字只从原文解析**，
    解析不到就留空 —— 这些数一旦是编的，投标价不是越限就是白送。
    """
    block_heading, block = _appendix_lines(tender_text)
    if not block:
        return "", ""
    heading = ""
    columns: list[str] = []
    rows: list[list[str]] = []
    total_cells: list[str] = []
    for line in block:
        if not heading and _APPENDIX_TITLE.search(_flat(line)) and len(line) <= 40:
            heading = line
            continue
        found = _quote_columns(line)
        if found and not columns:
            columns = found
            continue
        if not columns:
            continue
        cells = _split_cells(line)
        if not cells:
            continue
        if "合计" in _flat(line):
            total_cells = cells
            continue
        if re.match(r"^\d{1,3}$", cells[0]):
            rows.append(cells)
    if not columns:
        if not rows:
            return "", ""
        columns = ["序号", "名称", "型号规格", "数量", "限价（元）", "单价（元）", "合价（元）"]
    headers = ["| " + " | ".join(columns) + " |",
               "|" + "|".join(["---"] * len(columns)) + "|"]
    body: list[str] = []
    for row_no, cells in enumerate(rows, start=1):
        body.append("| " + " | ".join(_quote_row(cells, columns, row_no)) + " |")
    if not body:
        # 原文只给了空表（列名，没有数据行）：按一行的框架给待填，限价列留空
        body.append("| " + " | ".join(
            ["1"] + ["" if _limit_col(col) else "{{待填}}"
                     for col in columns[1:]]) + " |")
    total = [""] * len(columns)
    if len(columns) > 1:
        total[1] = "合计"
    if len(columns) > 2 and _is_money(columns[-1]):
        total[-1] = "{{待填}}"
    # 原文合计行里**真给了数字**就照抄（只在合价/总价那一格）；没给就保留待填
    numbers = [cell for cell in total_cells if _NUMBER_CELL.match(cell.replace("，", ","))]
    if numbers and len(columns) > 2 and _is_money(columns[-1]):
        total[-1] = numbers[-1]
    return (block_heading or heading or "附录（报价明细）"), "\n".join(
        headers + body + ["| " + " | ".join(total) + " |"])


def quotation_appendix_table(tender_text: str) -> str:
    return quotation_appendix(tender_text)[1]


# 报价变量：**按原文用词**输出。询比采购/竞争性磋商写「响应总报价」，招标写「投标总报价」，
# 两者是同一个变量，但成稿里写成另一个项目的口径就是口径不一致（评标只会按原文件的词找）。
_PRICE_TOTAL = re.compile(r"([\u4e00-\u9fff]{0,4}总报价)[ \t]*[（(]([^）)]{0,16})[）)]")
_PRICE_RATE = re.compile(r"(下浮率|浮动率|优惠率)[ \t]*[（(]?[ \t]*([%％])?[ \t]*[）)]?")
_PRICE_WORD = ("响应", "投标", "报价", "磋商", "应答")


def price_variables(tender_text: str) -> list[str]:
    """招标原文响应函里的报价变量标签（按原文用词，原文没写的才用通用口径）。

    返回值形如 `["响应总报价（大写含税）", "下浮率（%）"]`：
    * 总报价必须出现 —— 评审价就是按这一格（大写含税）算的；
    * 下浮率/优惠率只在原文确实出现时才出。旧实现不管原文有没有都补一行
      「下浮率（%）」，实测 1365 万的设备采购招标根本没有下浮率口径，成稿的
      投标函却多出一行擅自增加的费用变量，评标时会被当成报价口径变更。
    """
    section = _letter_section(tender_text) or {}
    body = _flat(section.get("body") or "")
    source = body or _flat(tender_text)
    labels: list[str] = []
    for found in _PRICE_TOTAL.finditer(source):
        # 「愿意以人民币（大写）（¥ ）**的**投标总报价（含税）」：捕获到的前四个字
        # 会把「的」带进来，落进占位标签就成了「的投标总报价（含税）」。
        head = re.sub(r"^(?:的|其|我方|贵方|本)", "", found.group(1))
        label = head + ("（%s）" % found.group(2) if found.group(2) else "")
        if label not in labels:
            labels.append(label)
    # 最想要的是「大写含税」那一格：同一份原文里常同时出现「（含税）」「（大写含税）」
    totals = [label for label in labels if "总报价" in label]
    if not totals:
        word = next((word for word in _PRICE_WORD if word in source), "响应")
        totals = ["%s总报价（大写含税）" % word]
    total = next((label for label in totals if "大写" in label), totals[0])
    rates = [label for label in labels if "率" in label]
    if not rates:
        rate_word = _PRICE_RATE.search(source)
        if rate_word:
            rates = ["%s（%%）" % rate_word.group(1)]
    return [total, *rates[:1]]


def _variable_needles(label: str) -> tuple[str, ...]:
    """判断某个变量在正文里是否已经出现（按内核词比对，标点/括号写法不同也算）。"""
    if "总报价" in label:
        return ("总报价",)
    if "率" in label:
        return tuple(word for word in ("下浮率", "浮动率", "优惠率") if word in label) or ("率",)
    return (label,)


def response_letter_gaps(text: str, tender_text: str) -> list[str]:
    """响应函这一节还缺什么（原文承诺条款 / 两个报价变量 / 附录表）。"""
    body = str(text or "")
    gaps: list[str] = []
    clauses = letter_clauses(tender_text)
    if clauses and len(_numbered_items(body)) < len(clauses):
        gaps.append("原文承诺条款 %d 条" % len(clauses))
    for label in price_variables(tender_text):
        if not any(needle in body for needle in _variable_needles(label)):
            gaps.append(label)
    heading, table = quotation_appendix(tender_text)
    if table and "附录" not in body and "报价明细" not in body:
        gaps.append("附录（报价明细）表")
    return gaps


def _numbered_items(body: str) -> list[str]:
    """正文里已有的编号条目（用来核对"原文几条、成稿几条"）。"""
    found: list[str] = []
    for raw in str(body or "").splitlines():
        match = _CLAUSE.match(raw.strip(" 　\t"))
        if match and len(_flat(match.group(4))) >= 6:
            found.append(match.group(4).strip())
    return found


def ensure_response_letter_frame(text: str, tender_text: str,
                                 title: str = "") -> tuple[str, list[str]]:
    """格式章第一节（响应函/投标函/报价函）补全：正文骨架 + 附录表。

    只补不改：已有正文原样保留，缺的变量/附录追加在后面；齐全时逐字返回。
    「第一项同时含附录/报价明细」时正文与附录表两样都产出；原文没有附录的项目
    **不硬造附录**（`quotation_appendix` 返回空）。
    """
    if not response_letter_scope(title):
        return str(text or ""), []
    body = str(text or "")
    gaps = response_letter_gaps(body, tender_text)
    if not gaps:
        return body, []
    blocks: list[str] = [body.strip()] if body.strip() else []
    thin = len(re.sub(r"\s", "", body)) < 120
    if thin:
        # 只剩标题/占位：整段换成响应函骨架（原文承诺条款逐条 + 两个变量）
        blocks = [response_letter_body(tender_text)] if not body.strip() else blocks + [
            response_letter_body(tender_text)]
    else:
        extra: list[str] = []
        for label in price_variables(tender_text):
            if any(needle in body for needle in _variable_needles(label)):
                continue
            extra.append("%s：{{待填:%s}}" % (label, label))
        if extra:
            blocks.append("\n".join(extra))
    heading, table = quotation_appendix(tender_text)
    if table and "附录" not in body and "报价明细" not in body:
        blocks.append(heading + "\n\n" + table)
    return "\n\n".join(block for block in blocks if block.strip()).strip(), gaps


# ── 填写位统一成 {{待填:…}}：成稿里不许留裸下划线 ────────────────────────────
# 招标原文的格式是「填空题」：`响应总报价（大写含税）：________（¥______元）`。
# 照录下来就成了裸下划线，出件时既不是红色待填、也不进占位清单，等于把空白交上去。
# 统一转成平台占位语法（标签取前面的字段名），「值」这一层就永远是 {{待填:…}}。
_BLANK = re.compile(r"[_＿]{2,}|[（(][ \t\u3000]*[）)]")
_SLOT_TAIL = re.compile(
    r"([\u4e00-\u9fff（）()%¥￥A-Za-z0-9·．\.\-]{2,24})[ \t]*[：:]?[ \t]*$")
# 整行只有「合 计」「总 计」：合计行被 PDF 挤到了表外
_TOTAL_LINE = re.compile(r"^[ \t]*(?:合[ \t]*计|总[ \t]*计)[ \t]*[:：]?[ \t]*$")
_TABLE_LINE = re.compile(r"^[ \t]*\|")
_TOTAL_ROW = re.compile(r"^[ \t]*\|[ \t]*(?:合[ \t]*计|总[ \t]*计)[ \t]*\|")
_MARKER_ONLY = re.compile(r"^[\W_]+$")
# 函件正文的锚点：真出稿的响应函/投标函一定带这些字样之一。正文里已经有成篇内容
# （哪怕只是一张表）时不再塞承诺条款骨架 —— 只补缺，不改写已有内容。
_LETTER_ANCHOR = re.compile(r"我方|致[：:]|承诺|声明|总报价|下浮|浮动|优惠率")


def _slot_name(prefix: str) -> str:
    """填写位的标签：取它前面的字段名（「…响应总报价（大写含税）：____」→该字段名）。

    两处要洗：条款编号（`3．` 不是字段名）与 PDF 断行造成的**半个括号**
    （`响应总报价（大写含税` 少了右括号，补平才是一个能用的变量名）。
    """
    found = _SLOT_TAIL.search(str(prefix or ""))
    if not found:
        return "…"
    name = re.sub(r"^\d{1,2}[ \t]*[．.、)）]", "", found.group(1).strip())
    name = re.sub(r"^(?:我方|贵方|本单位|本公司|本)", "", name)
    name = name.strip("：:，,、。；;（）() ")
    opens = name.count("（") + name.count("(")
    closes = name.count("）") + name.count(")")
    if opens > closes:
        name += "）" * (opens - closes)
    if not name or _MARKER_ONLY.match(name):
        return "…"
    return name


def fill_blanks(text: str) -> str:
    """裸填写位（`____` / `（ ）`）→ `{{待填:…}}`；全角百分号归一成 `%`。

    幂等：已经转成 `{{待填:…}}` 的不会再动（里面没有下划线，也不是空括号）。
    """
    source = str(text or "").replace("％", "%")
    out: list[str] = []
    last = 0
    for found in _BLANK.finditer(source):
        out.append(source[last:found.start()])
        out.append("{{待填:%s}}" % _slot_name(source[:found.start()]))
        last = found.end()
    out.append(source[last:])
    return "".join(out)


def _appendix_in_letter(tender_text: str) -> bool:
    """原文的响应函这一节里**自带**附录/报价明细（而不是另起一节）。"""
    body = str((_letter_section(tender_text) or {}).get("body") or "")
    for raw in body.splitlines():
        text = _flat(raw)
        if not text or len(text) > 40:
            continue
        if _APPENDIX_TITLE.search(text) and not re.search(r"[。；;]", text):
            return True
    return False


def _ensure_total_row(text: str) -> str:
    """把被挤到表外的「合 计」收进表里（原文没有合计行就不硬造）。

    附录表的合计行是评委核价的那一行；掉到表格外面就成了正文里一句孤零零的「合 计」。
    """
    source = str(text or "")
    lines = source.splitlines()
    rows = [index for index, line in enumerate(lines) if _TABLE_LINE.match(line)]
    if not rows:
        return source
    end = rows[-1]
    start = end
    while start - 1 in rows:
        start -= 1
    if any(_TOTAL_ROW.match(line) for line in lines[start:end + 1]):
        return source
    stray = [index for index, line in enumerate(lines)
             if _TOTAL_LINE.match(line) and not _TABLE_LINE.match(line)]
    if not stray:
        return source                    # 原文没给合计行：不硬造
    columns = [cell.strip() for cell in lines[start].strip().strip("|").split("|")]
    total = [""] * len(columns)
    total[1 if len(total) > 1 else 0] = "合计"
    kept = [line for index, line in enumerate(lines) if index not in set(stray)]
    rows = [index for index, line in enumerate(kept) if _TABLE_LINE.match(line)]
    kept.insert(rows[-1] + 1, "| " + " | ".join(total) + " |")
    return "\n".join(kept)


def ensure_format_letter(text: str, tender_text: str, title: str = "", *,
                         require_anchor: bool = False) -> tuple[str, list[str]]:
    """格式章的**第一节**（函件）+ 附录（报价明细）：缺什么补什么，齐全时逐字返回。

    为什么单列这一层：响应函/投标函/报价函/申请函是格式章的第一项，招标人就靠这一页
    认响应总报价与承诺条款；它在原文里**叫什么名字都行**（响应函/投标函/报价函/申请函），
    所以判断按语义（标题结尾是「函」）而不是按某个固定词。原文该节同时带附录/报价明细时，
    正文与附录表**两样都要有**；原文没有附录就绝不硬造。

    * 承诺条款条数按原文解析（`letter_clauses`），不写死；
    * 报价变量（{响应|投标}总报价（大写含税）、下浮率（%））一个都不能少；
    * 填写位统一成 `{{待填:…}}`（`fill_blanks`），不留裸下划线；
    * 附录表列名取自原文、行留 `{{待填}}`、合计行留在表内、金额一律不写死。

    `require_anchor`：整册后处理面对的可能是模型撰写的同题章节（`c1` 这种占位正文），
    没有函件/表格的任何痕迹就一个字都不动 —— 与 `ensure_format_subsections` 的参数同义。
    """
    body = str(text or "")
    # 原文里**根本没有**函件/附录原文时不凭空造：那等于替招标人编一页格式，
    # 与"照录格式、缺什么补什么"相反（整册后处理会把同题的自由章节也送进来）。
    if _letter_section(tender_text) is None and not quotation_appendix(tender_text)[1]:
        return body, []
    if require_anchor and not (_LETTER_ANCHOR.search(body) or _TABLE_LINE.search(body)):
        return body, []
    # 正文已经有成篇内容时只补缺的变量、不塞骨架；空正文（整章没出稿）才整页建。
    letter = response_letter_scope(title) and (
        not body.strip() or bool(_LETTER_ANCHOR.search(body)))
    appendix_here = letter and _appendix_in_letter(tender_text)
    if not (letter or appendix_scope(title) or appendix_here):
        return body, []
    gaps: list[str] = []
    if letter:
        body, gaps = ensure_response_letter_frame(body, tender_text, title)
        body = fill_blanks(body)
    heading, table = quotation_appendix(tender_text)
    if table and (appendix_here or appendix_scope(title)):
        if not any(_TABLE_LINE.match(line) for line in body.splitlines()):
            body = (body.rstrip() + "\n\n" + heading + "\n\n" + table).strip()
        elif appendix_scope(title):
            body = fill_blanks(body)
    return _ensure_total_row(body), gaps


def _present_subsections(body: str,
                         subs: list[dict[str, Any]]) -> dict[int, list[str]]:
    """正文里已经存在的小节段：序号 → 该段的行（标题按骨架核对，避免认错）。"""
    required = {int(sub["ordinal"]): sub for sub in subs}
    present: dict[int, list[str]] = {}
    for ordinal, lines in _split_subsections(body):
        if ordinal is None or ordinal not in required or ordinal in present:
            continue
        found = subsection_heading(lines[0] if lines else "")
        if not found or not _same_title(str(found["title"]), str(required[ordinal]["title"])):
            continue
        present[ordinal] = lines
    return present


def _repair_present(sub: dict[str, Any], lines: list[str],
                    tender_text: str) -> tuple[str, bool]:
    """已存在的小节：只补它自己的确定性缺口，已有文字逐字不动。

    两类缺口实测都会直接丢分/废标：

    1. **信誉/信用查询小节**：正文只有一段文字、渠道不全或没有图框。招标人点名几个渠道，
       就该有几行文字 + 几个图框（口径与 `credit_proofs` 完全一致）；不齐就按原文重建。
    2. **资格类材料**（设备修理/制造资质、基本账户开户许可证）：标题点了名，却只有一个
       文字说明、没有可替换的图框 —— 资格审查时等于没交这一页。
    """
    title = str(sub.get("title") or "")
    original = "\n".join(lines).strip()
    if credit_proof_scope(title):
        inner = "\n".join(lines[1:]).strip()
        if credit_proof_gaps(inner, tender_text):
            frame = _credit_frame(tender_text)
            if frame:
                heading = str(sub.get("heading") or "").strip() or title
                return (heading + "\n\n" + frame).strip(), True
    missing = qualification_missing(title, original)
    if missing:
        return (original.rstrip() + "\n\n" + "\n".join(missing)).strip(), True
    return original, False


def _subsections_needing_work(body: str, subs: list[dict[str, Any]],
                              tender_text: str) -> tuple[list[dict[str, Any]],
                                                         dict[int, tuple[str, bool]]]:
    """返回 (还要补的小节, {序号: (修好的正文, 是否改过)})。"""
    present = _present_subsections(body, subs)
    repairs: dict[int, tuple[str, bool]] = {}
    pending: list[dict[str, Any]] = []
    for sub in subs:
        ordinal = int(sub["ordinal"])
        lines = present.get(ordinal)
        if lines is None or not _has_content(lines):
            pending.append(sub)
            continue
        repaired, changed = _repair_present(sub, lines, tender_text)
        repairs[ordinal] = (repaired, changed)
        if changed:
            pending.append(sub)
    return pending, repairs


# ══════════════════════════════════════════════════════════════════════════
# 「格式1 投标函」清单章：13 个法定格式一个都不能少
# ══════════════════════════════════════════════════════════════════════════
# 实测事故（公开招标《招标文件2.11开标(1).pdf》，第四章《投标文件有关格式》第 36~46 页）：
# 原文用「格式1 投标函」「格式2-1 法定代表人身份证明书」「格式2-2 法定代表人授权书」
# …「格式12 投标保证金凭证」逐条列出 13 个法定格式，并按第二章第 8 条分成
# 「商务文件 / 资格文件 / 技术文件」三段。成稿 v9 里这 13 个格式一个都没有，
# 封面只写「投标文件（技术部分）」。
# 根因：`tender_checkup.format_chapter()` 只认标题里的固定词（「投标文件格式」），
# 「投标文件有关格式」认不出；`_SECTION_LINE` / `_catalog_keys` / `_section_marks`
# 又只认「一、」，认不出「格式N 名称」。识别见 `tender_checkup._catalog_list`；
# 这一层负责把条目落进成稿骨架，并**逐条带原文要点**（数值与约束只从原文取）。
_CATALOG_POINTS_HEAD = "原文要点（招标文件本章原文）："


def _catalog_block(entry: dict[str, Any]) -> str:
    """一个格式条目的小节块：标题 + 原文要点（没有可抽的约束就只留标题）。"""
    heading = str(entry.get("label") or "").strip()
    points = [str(point).strip() for point in entry.get("points") or []
              if str(point).strip()]
    if not points:
        return heading
    lines = [_CATALOG_POINTS_HEAD]
    for index, point in enumerate(points, start=1):
        lines.append("%d. %s" % (index, point))
    return heading + "\n\n" + "\n".join(lines)


def _catalog_scope(title: str, chapter_title: str) -> bool:
    """这一章的标题是不是「投标文件格式」章本身（整章骨架模式）。

    「投标文件格式」「投标文件有关格式」「响应文件的格式」都要认成同一件事 ——
    后者正是事故文件那个标题（固定词表认不出它）。
    """
    name = section_key(title)
    if not name:
        return True                      # 没给标题：按整章处理
    chapter = section_key(chapter_title)
    if not chapter:
        return False
    if name == chapter:
        return True
    if (name in chapter or chapter in name) and min(len(name), len(chapter)) >= 4:
        return True
    return "格式" in name and "文件" in name and "格式" in chapter and "文件" in chapter


def _catalog_heading_line(body: str, entry: dict[str, Any]) -> int:
    """正文里这一格式条目的标题行号（没有则 -1）。"""
    title = str(entry.get("title") or "")
    label = str(entry.get("label") or "")
    if not title and not label:
        return -1
    key = _flat(title)
    for index, raw in enumerate(str(body or "").splitlines()):
        text = raw.strip(" 　\t")
        if not text or len(text) > 90 or re.search(r"[。；;]", text):
            continue
        flat = _flat(text)
        if flat == _flat(label):
            return index
        if not _same_title(text, title):
            continue
        # 「格式1 投标函」/「投标函」都算这一节在；正文句子里顺带提到不算（长度卡住）
        if "格式" in flat or len(flat) <= len(key) + 8:
            return index
    return -1


def _catalog_targets(entries: list[dict[str, Any]], tender_text: str,
                     title: str) -> tuple[bool, list[dict[str, Any]]]:
    """这一章要补哪些格式条目：整章模式全部，条目模式只补同名那条。"""
    span = format_chapter(tender_text) or {}
    if _catalog_scope(title, str(span.get("title") or "")):
        return True, list(entries)
    name = str(title or "")
    picked = [entry for entry in entries
              if _same_title(name, str(entry.get("title") or ""))
              or _same_title(name, str(entry.get("label") or ""))]
    return False, picked


def _catalog_gaps(body: str, tender_text: str, title: str) -> list[str]:
    """格式清单章里还缺哪些条目（不是清单章返回 []）。"""
    entries = format_catalog(tender_text)
    if len(entries) < 3:
        return []
    _whole, targets = _catalog_targets(entries, tender_text, title)
    if not targets:
        return []
    return [str(entry.get("label") or "") for entry in targets
            if _catalog_heading_line(body, entry) < 0]


def ensure_format_catalog(text: str, tender_text: str, title: str, *,
                          require_anchor: bool = False) -> tuple[str, list[str]]:
    """格式清单章（格式1~格式12）：条目逐条落进骨架，并带原文要点。

    * 整章模式（标题就是「投标文件格式 / 投标文件有关格式」）：13 条全部补，段标题
      （资格文件 / 商务文件 / 技术文件 …）按原文顺序出，条目按段归位；
    * 条目模式（这一章就是「格式3 投标总报价表」）：只补同名那一条；
    * 已存在的标题不重复追加，只把缺的原文要点插到它后面（`_catalog_heading_line` 定位）；
    * 数值与约束全部来自原文（`tender_checkup._catalog_points`），抽不到就不写。
    """
    body = str(text or "")
    entries = format_catalog(tender_text)
    if len(entries) < 3:
        return body, []
    whole, targets = _catalog_targets(entries, tender_text, title)
    if not targets:
        return body, []
    present = {str(entry.get("label") or "") for entry in targets
               if _catalog_heading_line(body, entry) >= 0}
    if require_anchor and not present:
        return body, []          # 模型自写的自由正文：没有一条格式痕迹就不硬塞
    added: list[str] = []
    # 1) 标题已在、要点缺失：把原文要点插到该标题之后（不重复标题，幂等）
    for entry in targets:
        label = str(entry.get("label") or "")
        if label not in present:
            continue
        points = [str(point).strip() for point in entry.get("points") or []
                  if str(point).strip() and _flat(str(point)) not in _flat(body)]
        if not points:
            continue
        index = _catalog_heading_line(body, entry)
        if index < 0:
            continue
        block = (["", _CATALOG_POINTS_HEAD]
                 + ["%d. %s" % (number, point) for number, point in enumerate(points, start=1)])
        lines = body.splitlines()
        body = "\n".join(lines[:index + 1] + block + lines[index + 1:])
        added.append(label)
    # 2) 缺条目的：按原文顺序追加；整章模式先出段标题（三段结构落到成稿目录/说明里）
    missing = [entry for entry in targets if str(entry.get("label") or "") not in present]
    if not missing:
        return body, added
    blocks: list[str] = []
    shown: list[str] = []
    for entry in missing:
        part = str(entry.get("part") or "") if whole else ""
        if part and part not in shown:
            shown.append(part)
            if _flat(part) not in _flat(body):
                blocks.append(part)
        blocks.append(_catalog_block(entry))
        added.append(str(entry.get("label") or ""))
    text_out = "\n\n".join([body.strip()] + [block for block in blocks if block.strip()])
    return text_out.strip(), added


def format_subsections_gaps(text: str, tender_text: str, title: str) -> list[str]:
    """这一节还缺哪些**原文有、正文没有（或只有文字、缺图框）**的二级小节。"""
    body = str(text or "")
    catalog = _catalog_gaps(body, tender_text, title)
    if catalog:
        return catalog
    subs = format_sub_sections(tender_text, title)
    if len(subs) < 2:
        return []
    pending, _repairs = _subsections_needing_work(body, subs, tender_text)
    return [str(sub.get("title") or "") for sub in pending]


def ensure_format_subsections(text: str, tender_text: str, title: str, *,
                              require_anchor: bool = False) -> tuple[str, list[str]]:
    """把这一节按招标原文补全；返回 (正文, 新补齐的部分)。

    两层，都幂等、都只补不改：

    1. **格式章第一节**（响应函/投标函/报价函/申请函…）+ 附录（报价明细）：
       承诺条款条数按原文、两个报价变量、填写位 `{{待填:…}}`、附录表（列名取自原文、
       合计行留在表内）。这一层与「（一）（二）」二级小节无关，所以必须先做 ——
       第一节整项丢失时，下面那层根本没有东西可补（见 `ensure_format_letter`）。
    2. **二级小节**（（一）~（八））：原文有哪几个就补哪几个（资格审查资料的法定八项）。

    参数
    ----
    - `text`：当前正文（结构化直出或启发式渲染的结果，也可能已经是补齐过的）；
    - `tender_text`：招标文件正文（小节清单与评分口径都从这里取，**不写死**）；
    - `title`：该章标题（用来在格式章里定位父节，带不带编号都能认）；
    - `require_anchor`：要求正文里至少已经有一个原文小节才补。生成链路自己知道
      "这一章就是照录格式章"，传 False；整册后处理面对的可能是模型撰写的同题章节，
      传 True 才不会把八个小节硬塞进一篇自由正文。

    幂等：齐全时**逐字返回原正文**；重复调用不会重复生成、不会重排已有小节。
    """
    body = str(text or "")
    from zhibiao.core.bid_integrity import requires_authored_content
    if requires_authored_content(title):
        return body, []
    if not str(tender_text or "").strip():
        return body, []
    if require_anchor and not body.strip():
        return body, []
    # 第一节（函件）先补：它缺的不是"某个小节"，而是整页正文。
    body, letter_added = ensure_format_letter(body, tender_text, title,
                                             require_anchor=require_anchor)
    # 「格式1 投标函」清单章（第四章《投标文件有关格式》那种）：条目本身就是格式小节，
    # 原文有几条就补几条，且每条带原文要点 —— 与下面「（一）（二）」那一层无关，先做。
    body, catalog_added = ensure_format_catalog(body, tender_text, title,
                                                require_anchor=require_anchor)
    subs = format_sub_sections(tender_text, title)
    # 只有一个「（一）」的不当清单：那多半是文书内部的条款分组，不是同级小节。
    if len(subs) < 2:
        return body, letter_added + catalog_added
    # **空正文不再早退**：原文有 ≥2 个同级小节时，"整章从零补"就是把这一节按原文重建
    # （标题 + 框架/图框，一节都不少）。旧写法 `if not body.strip(): return body, []`
    # 让这一入口永远返回空串 —— 实测《新3.pdf》资格审查资料整章补全时，
    # 「（二）信用中国…查询记录」根本走不到，图框 0 个。整册后处理那条路有
    # `require_anchor=True` 兜底，不会被塞进模型撰写的自由正文。
    present = _present_subsections(body, subs)
    if require_anchor and not present:
        return body, letter_added + catalog_added
    missing, repairs = _subsections_needing_work(body, subs, tender_text)
    if not missing:
        return body, letter_added + catalog_added

    pieces = _split_subsections(body)
    preamble = "\n".join(pieces[0][1]).strip() if pieces and pieces[0][0] is None else ""
    blocks: list[str] = [preamble] if preamble else []
    used: set[int] = set()
    added: list[str] = []
    for sub in subs:
        ordinal = int(sub["ordinal"])
        used.add(ordinal)
        lines = present.get(ordinal)
        if lines is not None and _has_content(lines):
            repaired, changed = repairs.get(ordinal, ("\n".join(lines).strip(), False))
            blocks.append(repaired)     # 已有的原样保留，只可能追加了缺的图框
            if changed:
                added.append(str(sub.get("title") or ""))
            continue
        frame = _frame_body(sub, tender_text)
        heading = str(sub.get("heading") or "").strip() or str(sub.get("title") or "")
        block = (heading + "\n\n" + frame).strip() if frame else heading
        blocks.append(block)
        added.append(str(sub.get("title") or ""))
    # 正文里对不上清单的段（原文里可能也有）原样保留在末尾：补全绝不删内容
    for ordinal, lines in pieces:
        if ordinal is None or ordinal in used:
            continue
        tail = "\n".join(lines).strip()
        if tail:
            blocks.append(tail)
    return ("\n\n".join(block for block in blocks if block.strip()).strip(),
            letter_added + catalog_added + added)
