"""出件守卫：需要确认的表述、占位符/伪数据、必要章节、生成范围（大纲模式）。

实现来源（服务器容器 `/app/services/generate/skills/`）：
- 80 端口 `norm_rules.py`：`PLACEHOLDER_PATTERNS` / `SOFT_PLACEHOLDER_KINDS` /
  `REQUIRED_CHAPTER_KEYWORDS` / `_MODE_EXCLUDED_CHAPTERS` / `scan_bid_document`
- `outlineMode='standalone-technical'` 时
  「不得加入商务/资信、投标函、授权委托书等非技术章节」
- 80 端口 `bid_consistency.py`：修复幅度安全网（>40% 删减判定误伤，整章回退）

设计原则：本模块只做**确定性**判断 —— 不调用大模型、不臆断内容；命中即产出
结构化门禁条目，由导出接口转成 409（硬阻断项可被 force=1 覆盖）。
"""

from __future__ import annotations

import re
from typing import Any

# ══════════════════════ 1. 需要确认的表述（只给位置，不做审判） ══════════════════════
#
# 用户原话：「最后导出文件时候，不要有那么多的提示，最多就是告诉哪里有问题，
# 不要说不合规，不合规我还用这个软件干嘛」。所以**判定不变**（还是这份词表），
# 出口文案一律中性：只说「这句可以更正面一些 / 这个位置可以再看一眼」，
# 不出现「不合规 / 否决 / 无效投标 / 风险 / 违规」这类审判式说法。
# 前端文案集中在 frontend/src/App.tsx 的 EXPORT_HINT 常量里，改语气只动那一处。

# 单条提示的一句话说明（中性，只建议不判决）
EXPORT_HINT_SUGGESTION = "这句可以更正面一些"
# 有命中时接口返回的整体说明；无命中时为空串（前端据此完全不弹窗）
EXPORT_HINT_NOTE = "以下位置可以再看一眼，不影响导出。"

FORBIDDEN_BID_TERMS = (
    "废标",
    "否决投标",
    "无效投标",
    "投标无效",
    "否决响应",
    "响应无效",
    "投标文件无效",
    "无效响应",
    "投标被否决",
    "投标文件被否决",
    "被否决",
    "不予受理",
    "视为无效",
    "按无效处理",
)


def find_forbidden_bid_terms(text: str) -> list[str]:
    """Return disqualification terminology that must not appear in final body text."""
    value = str(text or "")
    return [term for term in FORBIDDEN_BID_TERMS if term in value]


# 照录招标格式的章节（投标函/身份证明/授权委托书/偏差表/分项报价表…）落库时，
# generation_runner 会把「这一章是照录件」写进 project_chapters.source（form_text 口径）。
# 命中任一标记即认定照录章；标记后面还可能拼「（招标人待填归一）」这类后缀。
_FORM_SOURCE_MARKS = ("格式文书", "格式原文", "按格式章节照录")


def is_format_form_chapter(source: str = "", title: str = "", tender_text: str = "") -> bool:
    """这一章是不是「照录招标文件格式」的章节（生成侧的 form_text 章节）。

    判定必须与生成侧同一个口径，否则就是「生成时放行、导出时拦下」的自相矛盾：
    1. `project_chapters.source` 带 form/legal 标记 —— 生成时 form_text=True 的章节必带；
    2. 人工编辑会把 source 覆盖成「人工编辑」，这时按标题二次判定：只认招标文件
       「投标文件格式」章里**确实存在格式原文**的小节（复用 format_chapter_text 的识别，
       技术方案/服务方案这类必须自己写的标题已被 _AUTHORED_HINTS 排除在外）；
    3. 拿不到招标原文返回 False —— 宁可让人看一眼，也不放过真正的自否定表述。
    """
    if any(mark in str(source or "") for mark in _FORM_SOURCE_MARKS):
        return True
    name = str(title or "").strip()
    if not name or not str(tender_text or "").strip():
        return False
    from zhibiao.core.format_forms import format_chapter_text  # 局部导入：避免模块级循环引用

    return format_chapter_text(name, tender_text) is not None


_SENTENCE_END = "。；！？!?;\n"


def _sentence_at(text: str, position: int, limit: int = 60) -> str:
    """取 position 所在的那一句（收掉换行与多余空白），用于「到底改哪一句」。"""
    value = str(text or "")
    if position < 0:
        return ""
    start = 0
    for char in _SENTENCE_END:
        start = max(start, value.rfind(char, 0, position) + 1)
    ends = [end for end in (value.find(char, position) for char in _SENTENCE_END) if end >= 0]
    end = min(ends) + 1 if ends else len(value)
    sentence = re.sub(r"\s+", " ", value[start:end]).strip()
    if len(sentence) <= limit:
        return sentence
    keep = max(8, limit - 2)          # 首尾省略号各占 1 字，总长仍不超过 limit
    before = len(re.sub(r"\s+", " ", value[start:position]))
    left = max(0, min(before - keep // 3, len(sentence) - keep))
    return ("…" if left else "") + sentence[left:left + keep].strip() + "…"


# 原文摘录上限：用户要求「原文摘录 ≤40 字，用引号」，够看清是哪一句即可
QUOTE_MAX_CHARS = 40


def _paragraph_index(text: str, position: int) -> int:
    """position 落在第几段（按非空行计），供前端显示「第 3 段」。"""
    value = str(text or "")
    if position <= 0:
        return 1
    head = value[:position]
    # 当前行从最后一个换行后开始：只数**前面完整的非空行**，命中行的半截不算
    line_start = head.rfind("\n") + 1
    return 1 + sum(1 for line in head[:line_start].splitlines() if line.strip())


def forbidden_bid_term_warnings(chapters: Any, tender_text: str = "",
                                numbers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """导出前定位「可以更正面一些」的句子：**只给位置，不判决、不拦截**。

    用户实测曾被挡在导出口外（《分项报价表》里招标原文自带「投标将被否决」），
    又明确要求「不要有那么多的提示，最多就是告诉哪里有问题，不要说不合规」。
    所以这里只产出**中性定位**清单：位置 + 原句，由前端弹窗列出来，
    用户点「直接导出」照常出件。提示走只读接口 `GET /projects/{id}/export-check`
    （见 pipeline_api）。判定口径（词表、照录章识别）一个字没动。

    每条 note：
      chapter_id / chapter_title / chapter_number / label —— 定位到哪一章；
      paragraph / location —— 第几段，以及拼好的整行位置
        （`第 1.1 节《技术方案》· 第 3 段`，前端直接显示这一行）；
      quote —— 命中词所在的那句（≤ QUOTE_MAX_CHARS 字，带引号显示即可）；
      suggestion —— 一句中性说明（`这句可以更正面一些`）；
      terms —— 命中的词（原句事实，仅供排查）；form_chapter / editable ——
        是否照录招标格式的章节（这类内容由招标人规定，通常不需要改）。
    """
    found: list[dict[str, Any]] = []
    numbering = numbers or {}
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        content = str(chapter.get("content") or "")
        terms = find_forbidden_bid_terms(content)
        if not terms:
            continue
        title = str(chapter.get("title") or "").strip()
        key = str(chapter.get("id") or "")
        number = str(numbering.get(key) or "") if key else ""
        position = content.find(terms[0])
        # 照录章也进清单（信息不藏），但打标记：原文自带的措辞不是投标人写错的
        form_chapter = is_format_form_chapter(str(chapter.get("source") or ""), title, tender_text)
        label = f"第 {number} 节《{title}》" if number else f"《{title}》"
        paragraph = _paragraph_index(content, position)
        found.append({
            "chapter_id": key,
            "seq": chapter.get("seq"),
            "chapter_title": title,
            "chapter_number": number,
            "label": label,
            "paragraph": paragraph,
            "location": f"{label}· 第 {paragraph} 段",
            "quote": _sentence_at(content, position, QUOTE_MAX_CHARS),
            "suggestion": EXPORT_HINT_SUGGESTION,
            "terms": terms,
            "form_chapter": form_chapter,
            "editable": not form_chapter,
        })
    return found


def repair_prompt(title: str, text: str, matched_terms: list[str]) -> str:
    terms = "、".join(matched_terms)
    return f"""请修订投标文件章节《{title}》的正文。

上一版出现了不应进入投标文件正文的否决/无效投标风险用语（{terms}）。招标要求只用于内部核对，正文应以投标人的肯定响应、执行措施和可核查承诺表达；不要转述风险条款、风险后果或自我否定结论。不得出现“废标、否决投标、无效投标、投标无效、否决响应、响应无效、投标文件无效”等词语及其变体。

【硬性约束：保持原数值口径】调用方会用本次输出**整章替换**原正文，所以只允许消除否决性表述，其余内容一律照抄：
- 保持原文中的数值、计量单位、期限（天数/月数）、起算条件、对象名称和标准号（含标准编号与年份）完全不变，例如「90 日历天」「24 个月」「GB/T 25974」「招标人现场负责人」一律原样保留；
- 不得把不同对象或不同起算条件的数值互相替换，不得换算、不得四舍五入、不得改写成「按技术协议执行」等模糊表述；
- 不得删减有效内容，也不得为了去掉风险用语而缩短篇幅：原正文的应答点必须逐条保留，只允许改写句子，不允许整段删除。

保留原文中真实、可用的技术和服务内容；删除或改写风险分析句。不得新增没有依据的事实、数字、承诺或企业信息。资料缺失时使用 {{{{待填:字段名称}}}} 占位。只输出修订后的完整正文。

【待修订正文】
{text}"""


# ══════════════════════ 2. 占位符与伪数据 ══════════════════════
#
# 必须由投标人填真实数据的项，模型只能留可检索占位，不能编造。
# 前 3 类（专利号占位 / 专利号通配 / 业绩虚构）是**硬阻断**：
# 编造专利号或业绩一旦随标书提交，通常直接触发否决投标。
# 「业绩虚构」不能只看「某 + 主体」：泛指句（「不得以任何方式指定某单位」「投标人应提供
# 某公司出具的授权书」）本身不构成编造业绩，只按正则硬拦会把正常表述判成废标级风险。
# 只有「某 + 主体」与业绩语义出现在**同一句话**里，才算编造业绩（判定见
# `_iter_fake_achievement_matches`）。
_FAKE_ACHIEVEMENT_KIND = "业绩虚构"
_FAKE_ACHIEVEMENT_SUBJECT_RE = r"某(?:煤矿|公司|集团|单位|企业|医院|学校|电厂|矿)"

PLACEHOLDER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("专利号占位", r"ZL?\d{4}\d{2}X{2,}[0-9X.]*"),
    ("专利号通配", r"\b[A-Z]{2}\d{4,}X{3,}"),
    (_FAKE_ACHIEVEMENT_KIND, _FAKE_ACHIEVEMENT_SUBJECT_RE),
    # 插图占位框（新写法 `{{图片占位:…}}` 与旧写法「此处需插入图片」）是平台**有意**
    # 留给用户贴图的预留位置，只提示、不硬拦（见 SOFT_PLACEHOLDER_KINDS），
    # 否则一份正文正常的标书会因为「图还没贴」被导出闸门整个挡住。
    ("图片占位", r"此处需插入图片|\{\{图片占位[:：]"),
    ("空待填", r"[\[【]\s*(?:待填|待补充|待生成|TBD|TODO)\s*[\]】]"),
    ("金额空值", r"\|\s*[—–]\s*\|"),
    ("未替换占位", r"【[^】]{0,20}(?:待填|请填|需填|未提供)[^】]{0,20}】"),
    ("待填标记", r"\{\{\s*待填\s*[:：][^}]{0,30}\}\}"),
)

# 这些属于「平台有意留给用户替换」或语义有歧义的占位，只提示不硬拦。
SOFT_PLACEHOLDER_KINDS: frozenset[str] = frozenset({
    "图片占位", "待填标记", "金额空值", "空待填", "未替换占位",
})

# 业绩语义词：只有与「某 + 主体」同句出现，才说明这是在讲「我们做过某家的项目」。
# 刻意不收录「指定」「出具」「授权」这类泛指动词，也不收录裸的「建设」「实施」「同类」——
# 它们在技术叙述里太常见，会把正常表述重新变成误报（宁可漏改不可错改）。
_ACHIEVEMENT_HINTS: tuple[str, ...] = (
    "已完成", "已签订", "已实施", "已交付", "已承建", "已投运", "曾为", "已为",
    "承建", "类似项目", "类似工程", "类似业绩", "同类项目", "同类工程",
    "业绩", "合同", "供货", "案例",
)

# 非断言只看当前主体所在分句的语境；不因同句后面出现「要求/承诺」而放过真实业绩。
_NON_ASSERTED_ACHIEVEMENT_PREFIX_RE = re.compile(
    r"将为|将于|将来|拟为|拟于|拟建设|拟承建|计划|打算|未来|今后|承诺|中标后|待中标"
    r"|尚未|并未|未曾|未为|未向|未建设|未实施|未部署|未完成|未承建|未交付"
    r"|没有|并非|不是|不曾|并不|不应|不得|不能|禁止|严禁|否认"
    r"|如果|假如|假设|若|倘|如(?:为|由|曾|已|我方|我公司|本公司)"
    r"|例如|示例|举例|引用原文|请填写|请提供|是否"
    r"|应当|应提供|应提交|应说明|应填写|需提供|须提供"
    r"|(?:招标文件|招标人|采购人)(?:明确)?(?:要求|规定)"
)
# 仅明确句首的示例/引述/条件前缀可跨逗号继承作用域，而不是任意同句词语。
_ACHIEVEMENT_CONTEXT_LEAD_RE = re.compile(
    r"^[ \t#>*\-0-9.、（）()：:]*"
    r"(?:例如|示例|举例|引用(?:原文)?|原文|假设|假如|如果|若|倘若"
    r"|请填写|请提供|(?:招标文件|招标人|采购人)(?:明确)?(?:要求|规定))"
)
_NON_ASSERTED_ACHIEVEMENT_SUFFIX_RE = re.compile(
    r"(?:的说法|的表述)?不属实|未发生|并不真实|纯属假设|仅(?:是|为)(?:示例|样例|假设)|吗[？?]?$"
)


def _quoted_achievement_subject(sentence: str, position: int) -> bool:
    """只豁免引号内的主体；项目名称加引号不影响引号外的事实断言。"""
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"', "'": "'"}
    stack: list[str] = []
    for char in sentence[:position]:
        if stack and char == stack[-1]:
            stack.pop()
        elif char in pairs:
            stack.append(pairs[char])
    return bool(stack)


def _non_asserted_achievement_context(sentence: str, start: int, end: int) -> bool:
    """当前主体的局部分句是否为未来/否定/假设/转引，而非过去业绩断言。"""
    if _quoted_achievement_subject(sentence, start):
        return True
    clause_start = max(sentence.rfind("，", 0, start), sentence.rfind(",", 0, start)) + 1
    boundary = re.search(r"[，,]", sentence[end:])
    clause_end = end + boundary.start() if boundary else len(sentence)
    if _NON_ASSERTED_ACHIEVEMENT_PREFIX_RE.search(sentence[clause_start:start]):
        return True
    if _NON_ASSERTED_ACHIEVEMENT_SUFFIX_RE.search(sentence[end:clause_end]):
        return True
    lead = _ACHIEVEMENT_CONTEXT_LEAD_RE.match(sentence)
    return bool(lead and lead.end() <= start)


# 补足「我方为某医院建设了同类系统」这个没有旧词表提示词的明确过去业绩句式。
# 必须同时有：第一人称施事 + 为某主体 + 完成体动词 + 同类/类似系统(平台)。
# 绝不把裸「建设」「为某」加进 _ACHIEVEMENT_HINTS；也不跨逗号或句号找宾语。
_PAST_FIRST_PERSON_ACHIEVEMENT_RE = re.compile(
    r"(?<![\w\u4e00-\u9fff])(?:我方|我公司|本公司|本单位|我们)[ \t]*"
    r"(?:(?:曾经|此前|先前|已经|去年|过去)[ \t]*)?为[ \t]*"
    r"(?P<subject>" + _FAKE_ACHIEVEMENT_SUBJECT_RE + r")[ \t]*"
    r"(?:建设|建成|部署|交付|实施)(?:了|过)[ \t]*(?:同类|类似)(?:系统|平台)"
)

_SENTENCE_BREAK_RE = re.compile(r"[。！？；;\n\r]")


def _sentence_span(text: str, start: int, end: int) -> str:
    """取 [start,end) 所在的整句，避免把相邻句子的业绩语义算到本句上。"""
    left = 0
    for m in _SENTENCE_BREAK_RE.finditer(text, 0, start):
        left = m.end()
    nxt = _SENTENCE_BREAK_RE.search(text, end)
    right = nxt.start() if nxt else len(text)
    return text[left:right]


def _iter_fake_achievement_matches(text: str) -> list[re.Match]:
    """泛指主体 + 同句明确业绩语义；非事实断言上下文宁可漏报。"""
    out: list[re.Match] = []
    for m in re.finditer(_FAKE_ACHIEVEMENT_SUBJECT_RE, text):
        sentence = _sentence_span(text, m.start(), m.end())
        left = 0
        for boundary in _SENTENCE_BREAK_RE.finditer(text, 0, m.start()):
            left = boundary.end()
        local_start, local_end = m.start() - left, m.end() - left
        if _non_asserted_achievement_context(sentence, local_start, local_end):
            continue
        # 核验本次「某主体」就是完成体句式的宾语，不能把同句另一个泛指主体也算进去。
        asserted_past = any(
            claim.start("subject") == m.start() - left
            for claim in _PAST_FIRST_PERSON_ACHIEVEMENT_RE.finditer(sentence)
        )
        if any(hint in sentence for hint in _ACHIEVEMENT_HINTS) or asserted_past:
            out.append(m)
    return out

# 生成失败的痕迹。这些文本一旦落库/导出，等于把「这一章没写出来」直接交给评委。
# 只认**系统痕迹形态**（带冒号、带括号、带「状态：」前缀、或整行独立），不认技术叙述里的
# 裸词「生成失败」：实测「报表生成失败时自动重试并记录日志」「数据生成失败率低于0.1%」
# 都是正常技术正文，按裸词匹配会把它们判成「生成失败痕迹」而硬拦导出。
FAILED_CONTENT_MARKERS: tuple[str, ...] = (
    "本章正文未能生成",
    "未能生成正文",
    "生成失败：",
    "生成失败:",
    "[生成失败]",
    "【生成失败】",
    "（生成失败）",
    "(生成失败)",
    "状态：生成失败",
    "状态:生成失败",
)

# 整行只剩「生成失败」同样是系统痕迹（正文被写坏成一行状态语）；技术句式不会整行只有这四个字。
_FAILED_LINE_RE = re.compile(r"(?m)^[ \t]*生成失败[。.]?[ \t]*$")

PH = "{{待填:%s}}"

# 同一件事的多种叫法 → 唯一规范名。人工补录时「查找替换」一次就能填完全部。
CANONICAL_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "投标总报价小写": (
        "投标总报价小写", "投标总报价金额", "投标总报价", "投标总价小写", "投标总价",
        "投标报价", "投标报价金额", "投标报价小写", "总报价", "报价金额", "报价合计",
    ),
    "投标总报价大写": (
        "投标总报价大写", "投标总价大写", "投标报价大写", "报价大写", "总价大写",
        "大写金额",
    ),
    "投标有效期": ("投标有效期", "响应有效期", "报价有效期", "投标有效期限"),
    "质保期": ("质保期", "保修期", "质量保证期", "质保期限", "免费质保期"),
    "服务期限": ("服务期", "服务期限", "履约期限", "合同期限", "服务年限"),
    "工期": ("工期", "交货期", "交付期", "完成期限", "项目工期", "建设周期"),
    "响应时限": ("响应时间", "响应时限", "售后响应时间"),
    "到场时限": ("到场时间", "到场时限"),
    "项目负责人": ("项目负责人", "项目经理", "项目负责人姓名", "拟派项目负责人", "拟派项目经理"),
    "联系人": ("联系人", "项目联系人", "业务联系人"),
    "联系电话": ("联系电话", "联系方式", "电话", "手机号", "手机号码"),
    "投标人名称": ("投标人名称", "投标单位名称", "公司名称", "单位名称", "供应商名称"),
    "统一社会信用代码": ("统一社会信用代码", "信用代码", "社会信用代码", "营业执照号"),
    "法定代表人": ("法定代表人", "法人代表", "法人姓名", "法定代表人姓名"),
    "开户银行": ("开户银行", "开户行", "基本户开户行"),
    "银行账号": ("银行账号", "账号", "银行账户", "基本户账号"),
    "税率": ("税率", "增值税税率", "适用税率"),
    "不含税报价": ("不含税报价", "不含税金额", "不含税价", "税前报价"),
    "税额": ("税额", "增值税额", "税金"),
}

_VARIANT_TO_CANON: dict[str, str] = {}
for _canon, _variants in CANONICAL_PLACEHOLDERS.items():
    for _variant in _variants:
        _VARIANT_TO_CANON[_variant] = _canon

_PH_RE = re.compile(r"\{\{\s*待填\s*[:：]\s*([^}]{1,40}?)\s*\}\}")

# Only full semantic aliases may be unified; unknown fields retain their scope.
def _fuzzy_canon(name: str) -> str | None:
    # Unknown fields are independent by default. Substring guessing cannot tell
    # a bidder's incorporation date from its name, or an owner's phone from ours.
    # Retain only complete, unambiguous aliases beyond the explicit dictionary.
    if re.fullmatch(r"(?:本次)?投标(?:总)?报价(?:金额)?(?:[（(])?大写(?:[)）])?", name or ""):
        return "投标总报价大写"
    if re.fullmatch(r"(?:本次)?投标(?:总)?报价(?:金额)?(?:[（(])?小写(?:[)）])?", name or ""):
        return "投标总报价小写"
    return {"投标不含税报价": "不含税报价", "不含税总报价": "不含税报价",
            "税前总报价": "不含税报价", "投标税额": "税额", "投标总税额": "税额",
            "增值税税额": "税额"}.get(name)


def find_placeholders(text: str) -> list[dict]:
    """返回 [{kind, count, samples}]，用于导出前门禁与前端清单。"""
    out: list[dict] = []
    if not text:
        return out
    for kind, pattern in PLACEHOLDER_PATTERNS:
        matches = (
            _iter_fake_achievement_matches(text)
            if kind == _FAKE_ACHIEVEMENT_KIND
            else list(re.finditer(pattern, text))
        )
        if not matches:
            continue
        out.append({
            "kind": kind,
            "count": len(matches),
            "samples": [
                text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")
                for m in matches[:3]
            ],
        })
    return out


def find_failed_markers(text: str) -> list[str]:
    """返回正文里出现的「生成失败」系统痕迹（不匹配技术叙述里的裸词）。"""
    value = str(text or "")
    out = [marker for marker in FAILED_CONTENT_MARKERS if marker in value]
    out.extend(match.group(0).strip() for match in _FAILED_LINE_RE.finditer(value))
    return out


def canonicalize_placeholders(text: str) -> tuple[str, int]:
    """把 `{{待填:投标总价}}` 之类统一成规范名。返回 (新文本, 改动数)。"""
    n = 0

    def sub(m: re.Match) -> str:
        nonlocal n
        name = m.group(1).strip()
        canon = _VARIANT_TO_CANON.get(name) or _fuzzy_canon(name)
        if canon and canon != name:
            n += 1
            return PH % canon
        return m.group(0)

    return _PH_RE.sub(sub, text or ""), n


# ══════════════════════ 3. 生成范围（大纲模式） ══════════════════════
#
# `outlineMode='standalone-technical'`：技术标独立成册时，
# 按定义就不该有投标函、法定代表人身份证明、开标一览表/报价表、授权委托书、
# 资格证明文件、商务响应 —— 若无条件要求这些，技术标项目会被永远 409 拦住。
OUTLINE_MODES: tuple[str, ...] = (
    "response-file",
    "partial",
    "standalone-technical",
    "standalone-business",
)

OUTLINE_MODE_LABELS: dict[str, str] = {
    "response-file": "完整响应文件",
    "partial": "部分章节",
    "standalone-technical": "技术标独立成册",
    "standalone-business": "商务标独立成册",
}

# 生成范围 -> 该范围下**不适用**的章节（既不硬拦也不提示）
_MODE_EXCLUDED_CHAPTERS: dict[str, tuple[str, ...]] = {
    "response-file": (),
    "partial": (),
    "standalone-technical": (
        "投标函", "法定代表人身份证明", "开标一览表/报价表",
        "授权委托书", "资格证明文件", "商务响应",
    ),
    "standalone-business": ("技术方案/服务方案", "售后服务"),
}

# 生成范围 -> 大纲生成时**禁止出现**的章节标题关键词（用于过滤目录）
MODE_FORBIDDEN_TITLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "response-file": (),
    "partial": (),
    "standalone-technical": (
        "投标函", "授权委托书", "法定代表人身份证明", "法人身份证明",
        "资格证明", "资格审查", "资质证明", "商务响应", "商务部分",
        "商务条款", "商务标", "开标一览表", "报价一览表", "投标报价表",
        "报价明细", "投标总价",
        # 第六章格式里的纯商务/法务附件：技术部分成册时不该出现（含用户按第六章
        # 一～十三直接建大纲时的「联合体协议书」「投标保证金」「配件采购承诺书」）。
        "投标保证金", "联合体协议", "联合体投标", "采购承诺书", "供应商配件采购承诺书",
        "分项报价表", "投标报价一览表",
    ),
    "standalone-business": ("技术方案", "服务方案", "技术部分", "实施方案", "售后服务"),
}


def export_scope_identity(mode: str, draft: bool = False, *, document: bool = False) -> tuple[str, str]:
    """Visible scope/draft identity; technical scope is not a complete bid.

    ``document=True`` 是**导出正文口径**：标题里不带「（草稿）」。编制状态是平台内部信息，
    写进封面第二行（「投标文件（草稿）」）递给评委就是自曝；草稿提示仍放在返回的 notes 里，
    界面/日志照旧可见。界面标签用默认口径（带「（草稿）」）即可。
    """
    labels = {
        "response-file": "投标文件",
        "standalone-technical": "投标文件（技术部分）",
        "standalone-business": "投标文件（商务部分）",
        "partial": "投标文件（部分章节）",
    }
    title = labels.get(mode, "投标文件")
    notes = []
    if mode == "standalone-technical":
        notes.append("编制范围：技术部分，不含完整商务报价、资格审查和其他法定附件，不能单独作为全套投标文件递交。")
    elif mode == "standalone-business":
        notes.append("编制范围：商务部分，不含完整技术响应，需按招标文件要求与其他部分组装。")
    elif mode == "partial":
        notes.append("编制范围：部分章节，不是完整投标文件。")
    if draft:
        if not document:
            title += "（草稿）"
        notes.append("当前为明确确认后导出的草稿，可能存在未完成章节、未解决的质量问题或待补材料，须逐项核对后方可用于正式投标。")
    return title, "\n".join(notes)


def normalize_outline_mode(value: Any = None) -> str:
    mode = str(value or "").strip()
    return mode if mode in OUTLINE_MODES else "response-file"


_TECH_TITLE_HINTS = ("技术", "方案", "施工", "服务", "实施", "竣工", "运维", "质量", "安全", "进度")
_BUSINESS_TITLE_HINTS = ("投标函", "报价", "开标一览", "授权委托", "法定代表人", "资格", "商务", "资信")

# 只有出现这些**硬性商务章节标题**，才足以判定「完整响应文件」。
# 「资格审查资料」「商务响应」这类词太泛：技术标分册里也常有「资格审查资料」，单凭它们
# 把技术标判成完整响应文件，就会硬报缺「投标函/法定代表人身份证明」并把导出拦成 409。
_HARD_RESPONSE_TITLE_HINTS = (
    "投标函", "开标一览", "报价一览", "投标报价表", "法定代表人身份证明", "法人身份证明",
    "投标保证金", "资格审查申请函", "授权委托书",
)
# 商业性标记数量达到该值时，也认为这是完整响应文件（覆盖「投标函+报价+资格」这类组合）。
_HARD_RESPONSE_MARKER_COUNT = 3


def detect_outline_mode(titles: list[str]) -> str:
    """按目录自动识别生成范围，避免「技术标独立成册」项目被必要章节门禁误拦。

    80 端口的历史教训：必要章节清单一刀切要求投标函/报价表，导致技术标独立成册的
    项目**永远被 409 拦住**，除了强制导出没有出路。这里用目录特征自动判断，
    既不引入新字段、也不会误拦。

    判定 response-file 需要**更强的证据**：出现硬性商务章节标题，或商业性标记 ≥3 个；
    「资格审查资料」「商务响应」与「技术方案」并列时不足以证明这是完整响应文件，
    按技术标独立成册处理（否则又会回到「除了强制导出没有出路」）。
    """
    joined = "\n".join(str(t or "") for t in titles or [])
    if not joined:
        return "response-file"
    business_markers = [key for key in _BUSINESS_TITLE_HINTS if key in joined]
    has_business = bool(business_markers)
    has_tech = any(key in joined for key in _TECH_TITLE_HINTS)
    if has_tech and not has_business:
        return "standalone-technical"
    if has_business and not has_tech:
        return "standalone-business"
    if not has_tech and not has_business:
        return "response-file"
    hard_business = any(key in joined for key in _HARD_RESPONSE_TITLE_HINTS)
    if hard_business or len(business_markers) >= _HARD_RESPONSE_MARKER_COUNT:
        return "response-file"
    return "standalone-technical"


def filter_chapters_by_mode(chapters: list[Any], mode: str) -> tuple[list[Any], list[str]]:
    """按生成范围剔除不该存在的章节。返回 (保留的章节, 被剔除的标题)。

    剔除「标题命中禁止关键词」的顶层章节时，**连同它的下级一起剔除**：只删父章节会留下
    无父的孤儿（如剔除「商务响应」后残留 level-2 的「商务条款偏离表」），后续
    shape_chapters / 章节编号会把它挂到别的父章节下，等于凭空多出一个不该存在的章节。
    """
    forbidden = MODE_FORBIDDEN_TITLE_KEYWORDS.get(normalize_outline_mode(mode), ())
    if not forbidden:
        return list(chapters or []), []
    kept: list[Any] = []
    dropped: list[str] = []
    dropping_parent = False
    for chapter in chapters or []:
        title = str((chapter or {}).get("title") or "")
        level = int((chapter or {}).get("level") or 1)
        if level <= 1:
            dropping_parent = any(key in title for key in forbidden)
            if dropping_parent:
                dropped.append(title)
                continue
        elif dropping_parent:
            # 父章节已被剔除，其下级一并剔除，避免产生孤儿章节
            continue
        kept.append(chapter)
    return kept, dropped


# ══════════════════════ 4. 导出前门禁 ══════════════════════

# 立项门槛：单章字数低于此值视为「没写」
MIN_ACCEPTABLE_CHAPTER_CHARS = 80

# (标签, 目录标题关键词, 是否硬阻断)
REQUIRED_CHAPTER_KEYWORDS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("投标函", ("投标函",), True),
    ("法定代表人身份证明", ("法定代表人身份证明", "法人身份证明"), True),
    ("开标一览表/报价表", ("开标一览表", "报价一览表", "投标报价表", "报价明细", "投标总价"), True),
    ("授权委托书", ("授权委托书", "授权书"), False),
    ("资格证明文件", ("资格证明", "资格审查", "资质证明"), False),
    ("技术方案/服务方案", ("技术方案", "服务方案", "技术部分", "实施方案"), False),
    ("商务响应", ("商务响应", "商务部分", "商务条款", "商务标"), False),
    ("售后服务", ("售后服务", "服务承诺", "质保", "质量承诺"), False),
)


def scan_bid_document(
    chapters: list[dict],
    outline_mode: str = "response-file",
    check_items: list[dict] | None = None,
) -> dict:
    """导出前全量体检。返回可直接驱动门禁与前端提示的结构。

    chapters: [{"id","seq","title","content","status","words"}...]
    check_items: 可选，C01–C21 检查报告条目（只采信 engine 以 "AI" 开头的，
                 避免结构规则兜底项造成「永远被拦住」的假 409）

    分两档严重度：
      hard —— 硬阻断（force=1 才可导出）：生成失败、缺必选章节、伪数据/未替换占位、
              合规检查的 AI 判定项
      soft —— 提示但不拦：可能缺少的章节、待人工补录的占位、字数不足
    """
    hard: list[dict] = []
    soft: list[dict] = []
    rows = [c for c in (chapters or []) if isinstance(c, dict)]

    # ── 4.1 生成失败 / 空正文 ──
    failed = [
        f"第 {c.get('seq')} 节《{c.get('title')}》"
        for c in rows
        if str(c.get("status") or "") != "已生成" or not str(c.get("content") or "").strip()
    ]
    markers = [
        f"第 {c.get('seq')} 节《{c.get('title')}》（{ '、'.join(find_failed_markers(str(c.get('content') or ''))) }）"
        for c in rows
        if find_failed_markers(str(c.get("content") or ""))
    ]
    if failed:
        hard.append({
            "kind": "未完成章节",
            "detail": "以下章节没有正文或未标记为已生成，导出后评委看到的是空缺",
            "items": failed[:12],
        })
    if markers:
        hard.append({
            "kind": "生成失败痕迹",
            "detail": "正文中残留生成失败提示语，导出后会被原样提交",
            "items": markers[:12],
        })

    # ── 4.2 缺必要章节（按生成范围豁免）──
    titles = [str(c.get("title") or "") for c in rows]
    joined_titles = "\n".join(titles)
    excluded = set(_MODE_EXCLUDED_CHAPTERS.get(normalize_outline_mode(outline_mode), ()))
    missing_hard: list[str] = []
    missing_soft: list[str] = []
    for label, keys, is_hard in REQUIRED_CHAPTER_KEYWORDS:
        if label in excluded:
            continue
        if not any(key in joined_titles for key in keys):
            (missing_hard if is_hard else missing_soft).append(label)
    if missing_hard:
        hard.append({
            "kind": "缺必要章节",
            "detail": "以下章节在招标文件里通常是必备项，补齐后再导出更稳妥",
            "items": missing_hard,
        })
    if missing_soft:
        soft.append({
            "kind": "可能缺少的章节",
            "detail": "以下章节未在目录中出现；若招标文件未要求可不处理",
            "items": missing_soft,
        })

    # ── 4.3 占位符 / 伪数据（编造专利号·业绩）──
    full_text = "\n".join(str(c.get("content") or "") for c in rows)
    placeholders = find_placeholders(full_text)
    blocking = [p for p in placeholders if p["kind"] not in SOFT_PLACEHOLDER_KINDS]
    if blocking:
        hard.append({
            "kind": "占位符/伪数据",
            "detail": "正文里还有未替换的占位或无法核实的专利号/业绩，请先替换成真实内容",
            "items": [f"{p['kind']} × {p['count']}" for p in blocking],
            "samples": [s for p in blocking for s in p["samples"][:2]][:6],
        })
    soft_ph = [p for p in placeholders if p["kind"] in SOFT_PLACEHOLDER_KINDS]
    if soft_ph:
        soft.append({
            "kind": "待人工补录",
            "detail": "以下位置需要人工替换（插图位置 / 待填标记 / 空金额），导出后请务必逐处处理",
            "items": [f"{p['kind']} × {p['count']}" for p in soft_ph],
            "samples": [s for p in soft_ph for s in p["samples"][:1]][:4],
        })

    # ── 4.4 合规检查报告（C01–C21）──
    # 只采信真正做过的内容级判定（engine 以 "AI" 开头）。结构规则兜底项在未生成
    # 正文时会天天报「必须修改」，若也纳入门禁，项目会被永远拦住 —— 这正是 80 端口
    # 技术标独立成册「除了强制导出没有出路」的历史教训。
    ai_blocked = [
        f"{item.get('id')} {item.get('name')}：{str(item.get('detail') or '')[:80]}"
        for item in (check_items or [])
        if isinstance(item, dict)
        and str(item.get("status") or "") == "必须修改"
        and str(item.get("engine") or "").startswith("AI")
    ]
    if ai_blocked:
        hard.append({
            "kind": "合规检查必须修改",
            "detail": "检查报告里还有几项要求修改，可以先按报告改完再导出",
            "items": ai_blocked[:12],
        })
    elif check_items:
        soft.append({
            "kind": "合规检查已通过",
            "detail": "最近一次内容级合规检查没有「必须修改」项",
            "items": [],
        })

    # ── 4.5 字数不足（软提示）──
    thin = [
        f"第 {c.get('seq')} 节《{c.get('title')}》（{int(c.get('words') or 0)} 字）"
        for c in rows
        if str(c.get("status") or "") == "已生成"
        and 0 < int(c.get("words") or 0) < MIN_ACCEPTABLE_CHAPTER_CHARS
    ]
    if thin:
        soft.append({
            "kind": "章节字数不足",
            "detail": f"以下章节不足 {MIN_ACCEPTABLE_CHAPTER_CHARS} 字，可能被判为未实质响应",
            "items": thin[:12],
        })

    return {
        "hard": hard,
        "soft": soft,
        "blocking": bool(hard),
        "outline_mode": normalize_outline_mode(outline_mode),
        "chapter_count": len(rows),
        "placeholders": placeholders,
    }


def gate_message(report: dict) -> dict:
    """把体检结果转成可直接放进 409 detail 的结构化载荷。"""
    hard = report.get("hard") or []
    lines = [f"{item['kind']}：{'、'.join(item.get('items') or []) or item.get('detail', '')}"
             for item in hard]
    return {
        "code": "EXPORT_BLOCKED",
        "allow_force": True,
        "message": "导出前有这些地方要确认（确认后可以继续导出）：" + "；".join(lines)[:1200],
        "blockers": hard,
        "advisories": report.get("soft") or [],
    }
