"""正文卫生：把「不可能是标书内容」的文字从正文里清掉，并检出没写完的章节。

实测问题（用户评审指出）：
1. 模型把写作提示词当正文抄了进去：「参数、设备归属、数量、标准与期限以全书统一台账为准；
   台账已有的数据不得改成待填。」这类句子出现在 5.8 节末尾 —— 交给评委就是事故；
2. PDF/生成过程把「【一】年」变成「一 年」：中文数词与量词之间多出空格；
3. 章节开头先写一段「导言」，又把 10.1~10.6 的内容浓缩复述一遍 —— 与后文重复；
4. 章末在半句话处断掉（「我方按合同条款」直接结束），明显没写完。
5. 照录招标格式时把 PDF 页脚页码（落款「年 月 日」下面的 54、55）抄进正文；
6. 投标函「（13）其他资料」下面多出一行「……」的空白占位线；
7. 标题缺配对引号（「8.1 零整比”」），成稿目录里是半个引号；
8. 招标文件明确「不接受代理商投标」，正文却写「投标人为代理经销商时，同时附制造商
   相应资质材料」—— 等于自己把投标人写成不合格主体（见 `suppress_agency_assumptions`）；
9. PDF 每页都印的「页码 + 编制单位」页眉页脚被整行抄进正文（「第41页 某某项目管理
   有限公司编制」），旧清洗只认「第 X 页 共 Y 页」这种标准页码页脚，认不出这种组合
   （见 `strip_page_furniture`）。

这里只做**机械可判**的事：删指令句、收空格、删重复导言、判断尾、清页脚/占位行、
配平标题引号、按招标原文条件抑制代理商身份表述。不评价文笔，也不替模型补内容。
"""
from __future__ import annotations

import re

# 只可能是写作指令、不该出现在标书正文里的句子（命中即整句删除）
_PROMPT_LEAK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"参数、设备归属、数量、标准与期限以全书统一台账为准[。；;]?"),
    re.compile(r"台账已有的数据不得改成待填[。；;]?"),
    re.compile(r"（?台\s*账）?\s*已有的数据不得改成待填[。；;]?"),
    re.compile(r"以全书统一台账为准[。；;]?"),
    re.compile(r"不得改成待填[。；;]?"),
    re.compile(r"参数、设备归属、数量[^。\n]{0,20}台账[^。\n]{0,20}[。；;]?"),
)

# 只收中文数词与量词之间的空格（「一 年」→「一年」）；阿拉伯数字与单位之间的空格
# 是招标文件自己的排版习惯（「11 万元」「3.5 m/s」），不能动。
_UNIT_GAP = re.compile(r"(?<=[一二三四五六七八九十两])\s+(?=[年月日天台件套个条项只组张份次人])")

# 落款/签名行：这些行附近的孤立 1~3 位数字是 PDF 页脚页码，不是正文。
# 实测「法定代表人身份证明」「授权委托书」末尾的「年 月 日」下面还挂着一个「54」「55」，
# 照录进标书就是给评委看一份带页码残片的文书。
_SIGNATURE_HINT = re.compile(r"(?:年\s*月\s*日|投标人|供应商|法定代表人|法人代表|授权代表"
                             r"|委托代理人|代理人|日期[：:]|盖单位章|单位公章|签字|签章)")
_PAGE_NUMBER_LINE = re.compile(r"^[ \t　]*\d{1,3}[ \t　]*$")
_PAGE_FOOTER_LINE = re.compile(
    r"^[ \t　]*第\s*\d+\s*页(?:[ \t　]*[，,／/]?[ \t　]*共\s*\d+\s*页)?[ \t　]*$")
# 「年 月 日 54」：页码被 PDF 并进了落款行尾
_SIGNATURE_TAIL_PAGE = re.compile(r"^(?P<head>\S.*?)[ \t　]+(?P<page>\d{1,3})$")
_SIGNATURE_WINDOW = 3

# 整行只有省略号/点线（「……」「...」「．．．」「—」）：投标函「（13）其他资料」下面
# 多出的这一行是招标文件里的空白占位线，照录到成稿里像没写完。
_ELLIPSIS_ONLY_LINE = re.compile(
    r"^[ \t　]*(?:[.．·・]{2,}|…+|—+|–+|＿{2,}|_{2,}|＝{2,}|={2,}|\*{3,}|~{3,})[ \t　]*$")


def strip_prompt_leaks(text: str) -> tuple[str, bool]:
    """删掉混进正文的写作指令句；返回 (清理后文本, 是否改动)。"""
    body = str(text or "")
    if not body.strip():
        return body, False
    cleaned = body
    for pattern in _PROMPT_LEAK_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    if cleaned == body:
        return body, False
    # 删除句子后可能留下孤立的空行/多余标点
    cleaned = re.sub(r"[ \t　]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"^[，、；：\s]+", "", cleaned, flags=re.M)
    return cleaned.strip(), True


def fix_unit_gap(text: str) -> tuple[str, bool]:
    """收掉中文数词与量词之间的空格（「一 年」→「一年」「二 台」→「二台」）。"""
    body = str(text or "")
    cleaned = _UNIT_GAP.sub("", body)
    return cleaned, cleaned != body


def strip_page_numbers_near_signature(text: str) -> tuple[str, bool]:
    """删掉落款附近的孤立页码行（PDF 页脚被抄进正文）。

    判据只看机械信号：整行 1~3 位数字，且前后 3 行内出现落款关键词
    （年 月 日 / 投标人 / 法定代表人 / 授权代表 / 日期 / 盖章 / 签字…）。
    表格行不会被误删：表格行一定带「|」，不是纯数字行。
    """
    body = str(text or "")
    if not body.strip():
        return body, False
    lines = body.splitlines()
    keep = [True] * len(lines)
    changed = False
    for index, raw in enumerate(lines):
        line = raw.strip(" 　\t")
        if not line:
            continue
        if _PAGE_FOOTER_LINE.match(line):
            keep[index] = False
            changed = True
            continue
        if not _PAGE_NUMBER_LINE.match(line):
            continue
        low = max(0, index - _SIGNATURE_WINDOW)
        high = min(len(lines), index + _SIGNATURE_WINDOW + 1)
        if any(_SIGNATURE_HINT.search(lines[other]) for other in range(low, high)
               if other != index):
            keep[index] = False
            changed = True
    # 落款行尾被并进来的页码：「年 月 日 54」→「年 月 日」
    for index, raw in enumerate(lines):
        if not keep[index]:
            continue
        line = raw.rstrip()
        if not line or not _SIGNATURE_HINT.search(line):
            continue
        found = _SIGNATURE_TAIL_PAGE.match(line)
        if not found or re.search(r"\d", found.group("head")):
            continue
        lines[index] = found.group("head")
        changed = True
    if not changed:
        return body, False
    return "\n".join(line for index, line in enumerate(lines) if keep[index]), True


# ── PDF 页眉/页脚水印：「页码 + 代理机构/编制单位」───────────────────────────────
# 实测事故（成稿第 3 章、第 5 章残留 5 行采购文件页脚）：
#   第41页 山西华普项目管理有限公司编制
#   第44页 山西华普项目管理有限公司编制 …… 第47页 山西华普项目管理有限公司编制
# 这些行不是正文，是采购文件每页固定输出的「页码 + 编制/代理单位」水印，照录招标
# 原文时被整行抄进了成稿。旧清洗只认「第 X 页 共 Y 页」这种标准页码页脚，认不出
# 这种「页码 + 机构名」组合，于是漏进正文。判据全部机械可判，不写死任何公司名：
#   a. 行首/行尾是「第 N 页」「Page N」「- N -」这类页码；且
#   b. 同一行还含机构/行为词（编制/代理/咨询/有限公司/项目管理…）；且
#   c. 去空白后 ≤ 40 字（正文句子不会这么短且只有这些元素）；
#   d. 整行只有「第 N 页」或「第 N 页 共 M 页」的纯页码行，直接删（与 a/b/c 无关）。
# 另外做**全文一致性校验**防误删正文：把页码数字去掉后的「页脚指纹」在全文重复
# 出现 ≥2 处、且每处都是「页码 + 机构词」时，才按页脚指纹整类处理 —— 也只有这种
# 「同一句在多页上反复出现」的情形才放宽 c 的 40 字上限（最多到 80 字）；孤零零
# 一行的超长行一律保留：它更可能是正文引用，宁可漏删，不可误删正文。
_PAGE_TOKEN = r"(?:第\s*\d{1,3}\s*页|Page\s*\d{1,3}|[-–—]\s*\d{1,3}\s*[-–—])"
_PAGE_TOKEN_HEAD = re.compile(r"^[ \t　]*" + _PAGE_TOKEN, re.I)
_PAGE_TOKEN_TAIL = re.compile(_PAGE_TOKEN + r"[ \t　]*$", re.I)
# 「第41页的报价应包含运输费」：页码后面紧跟功能词，是正文在引用页码，不是页脚。
_PAGE_TOKEN_IN_SENTENCE = re.compile(
    r"^[ \t　]*" + _PAGE_TOKEN
    + r"[ \t　]*(?:的|是|为|应|须|需|可|将|在|由|按|和|与|及|中|上|下|内|前|后|至)")
# 「编制依据详见招标文件第41页」：页码前面是引用动词，同样是正文在引用页码，不是页脚。
_PAGE_REFERENCE_VERB = re.compile(
    r"(?:详见|参见|见|参考|依据|按照|按|根据|据|同|如|及|至|到"
    r"|第[一二三四五六七八九十百零\d]{1,4}[章节条款项])[^。；！？\n]{0,12}[ \t　]*$")
# 机构名 / 编制行为词。只认这些通用词，换个代理机构、换个项目一样命中。
_FURNITURE_ORG_WORD = re.compile(
    r"(?:编制|招标代理|采购代理|代理机构|代理|咨询|项目管理|有限责任公司|有限公司)")
# 纯页码行（d）：「第 7 页」「第 7 页 共 48 页」「Page 7」「- 7 -」
_PURE_PAGE_LINE = re.compile(
    r"^[ \t　]*(?:第\s*\d{1,3}\s*页(?:[ \t　]*[，,／/]?[ \t　]*共\s*\d{1,3}\s*页)?"
    r"|Page\s*\d{1,3}(?:[ \t　]*(?:of|/)[ \t　]*\d{1,3})?"
    r"|[-–—]\s*\d{1,3}\s*[-–—])[ \t　]*$", re.I)
_FURNITURE_MAX_CHARS = 40            # 单行判据 c
_FURNITURE_FINGERPRINT_MAX_CHARS = 80   # 全文重复的页脚指纹才放宽到这里
# 页脚指纹：先把页码、总页数、装饰符去掉，再把空白收干 —— 同一页脚在各页上只差页码。
_PAGE_DIGITS = re.compile(
    r"第\s*\d{1,3}\s*页|Page\s*\d{1,3}|共\s*\d{1,3}\s*页"
    r"|[-–—]\s*\d{1,3}\s*[-–—]|(?:of|/)\s*\d{1,3}|\d{1,3}", re.I)


def _tight_length(line: str) -> int:
    """去掉所有前后/行内空白后的字数（中文页脚常带排版空格，不能算进长度）。"""
    return len(re.sub(r"[ \t\r　]", "", str(line or "")))


def _page_furniture_fingerprint(line: str) -> str:
    """去掉页码数字、空白与装饰符后的「页脚指纹」（同一页脚各页只差页码）。"""
    text = _PAGE_DIGITS.sub("", str(line or ""))
    text = re.sub(r"[ \t\r　]+", "", text)
    return text.strip("-–—·．.、,，;；:：/／|()（）[]【】\"'“”")


def _has_page_token(line: str) -> bool:
    """行首或行尾是否就是页码（正文里「第41页的报价…」「详见招标文件第41页」不算）。"""
    stripped = str(line or "").strip(" \r　\t")
    if not stripped or _PAGE_TOKEN_IN_SENTENCE.match(stripped):
        return False
    if _PAGE_TOKEN_HEAD.search(stripped):
        return True
    tail = _PAGE_TOKEN_TAIL.search(stripped)
    if not tail:
        return False
    # 页码前面是「详见 / 见 / 依据 / 第 X 章」这类引用动词 → 正文在引用页码，保留
    return not _PAGE_REFERENCE_VERB.search(stripped[:tail.start()])


def _looks_like_page_furniture(line: str) -> bool:
    """必要条件 a+b（不含长度判据）：行首/行尾有页码，且整行还带机构/行为词。"""
    stripped = str(line or "").strip(" \r　\t")
    if not stripped or "|" in stripped:      # 表格行一律不动：表格里「第41页」是单元格内容
        return False
    return _has_page_token(stripped) and bool(_FURNITURE_ORG_WORD.search(stripped))


def strip_page_furniture(text: str) -> tuple[str, list[str]]:
    """剥掉 PDF 页眉/页脚水印（页码 + 代理机构/编制单位），返回 (文本, 移除行说明)。

    为什么要单独一条：成稿第 3、5 章残留了 5 行采购文件页脚
    「第41页 某某项目管理有限公司编制」，旧清洗只认「第 X 页 共 Y 页」，
    于是代理机构名跟着页码一起进了正文，评委一眼就能看出是照抄。

    判据（不写死公司名，任何代理机构都适用）：
    * a 行首/行尾是「第 N 页」「Page N」「- N -」；
    * b 同行还含「编制/代理/咨询/有限公司/项目管理…」这类机构或行为词；
    * c 去空白后 ≤ 40 字；
    * d 整行只有「第 N 页」/「第 N 页 共 M 页」的纯页码行直接删。
    防误删：① 表格行（含「|」）不删；② 页码后紧跟「的/是/应…」的正文引用不删；
    ③ 全文一致性校验 —— 只有当「去页码后的指纹」在全文重复 ≥2 处、且每处都满足
    a+b 时，才按页脚指纹整类处理（也只有此时放宽 c 到 80 字）；只出现一次的行必须
    同时满足 a+b+c 才删，否则保留。

    纯函数、幂等、不抛异常：不读写文件、不改全局状态，重复调用结果不变；删行后
    不留空行堆叠（连续空行收成一行空行），表格行与段落结构原样保留。
    """
    body = str(text or "")
    if not body.strip():
        return body, []
    try:
        return _strip_page_furniture(body)
    except Exception:            # 纯文本清洗失败不该把整章生成带崩：原样返回
        return body, []


def _strip_page_furniture(body: str) -> tuple[str, list[str]]:
    lines = body.split("\n")
    stripped = [line.strip(" \r　\t") for line in lines]

    # ① 先做全文一致性校验：同一页脚指纹出现在几行、每行是否都满足 a+b。
    fingerprints = [""] * len(lines)
    occurrences: dict[str, int] = {}
    for index, line in enumerate(stripped):
        if not _looks_like_page_furniture(line):
            continue
        fingerprint = _page_furniture_fingerprint(line)
        fingerprints[index] = fingerprint
        occurrences[fingerprint] = occurrences.get(fingerprint, 0) + 1
    repeated = {fingerprint for fingerprint, count in occurrences.items()
                if fingerprint and count >= 2}

    # ② 逐行判定：纯页码行（d）> 单行页脚（a+b+c）> 重复指纹放宽长度。
    notes: list[str] = []
    kept: list[str] = []
    for index, line in enumerate(stripped):
        if not line:
            kept.append(lines[index])
            continue
        if _PURE_PAGE_LINE.match(line):
            notes.append("第 %d 行删除纯页码页脚：%s" % (index + 1, line))
            continue
        if _looks_like_page_furniture(line):
            fingerprint = fingerprints[index]
            # 同指纹在全文重复出现才认「页脚指纹」，也才放宽长度上限；
            # 只出现一次的行仍按 40 字上限判 —— 长行宁可当正文保留。
            limit = (_FURNITURE_FINGERPRINT_MAX_CHARS
                     if fingerprint in repeated else _FURNITURE_MAX_CHARS)
            if _tight_length(line) <= limit:
                notes.append("第 %d 行删除页眉/页脚水印：%s" % (index + 1, line))
                continue
        kept.append(lines[index])
    if not notes:
        return body, []

    cleaned = "\n".join(kept)
    # 删掉整行后不留空行堆叠：连续空行收成一行空行（段落结构不变）
    cleaned = re.sub(r"\n[ \t　]*\n(?:[ \t　]*\n)+", "\n\n", cleaned)
    if not body.startswith("\n"):
        cleaned = cleaned.lstrip("\n")
    if body.endswith("\n") and not cleaned.endswith("\n"):
        cleaned += "\n"
    if cleaned == body:
        return body, []
    return cleaned, notes


def is_ellipsis_line(line: str) -> bool:
    """整行只有省略号/点线（表格行不算：表格里的「|---|」是分隔行）。"""
    text = str(line or "").strip(" 　\t")
    if not text or text.startswith("|"):
        return False
    return bool(_ELLIPSIS_ONLY_LINE.match(text))


def strip_ellipsis_lines(text: str) -> tuple[str, bool]:
    """删掉整行只有省略号/点线的行（招标文件里的空白占位线、目录点线残片）。

    只处理**整行**：句中的「……」不动；表格行（含「|」）不动，免得把表格分隔行删掉。
    """
    body = str(text or "")
    if not body.strip():
        return body, False
    kept: list[str] = []
    changed = False
    for raw in body.splitlines():
        if is_ellipsis_line(raw):
            changed = True
            continue
        kept.append(raw)
    if not changed:
        return body, False
    return "\n".join(kept), True


def fix_quote_pairing(text: str) -> str:
    """去掉没配对的孤立右引号（标题笔误「8.1 零整比”」缺左引号）。

    成对的「“…”」原样保留；多出来的那个右引号直接删掉 —— 标题里出现半个引号
    在成稿里非常显眼，而平台无法可靠猜出左引号该加在哪。
    """
    body = str(text or "")
    if "”" not in body:
        return body
    out: list[str] = []
    opened = 0
    for char in body:
        if char == "“":
            opened += 1
        elif char == "”":
            if opened <= 0:
                continue
            opened -= 1
        out.append(char)
    return "".join(out)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。；！？])", str(text or ""))
    return [p.strip() for p in parts if len(p.strip()) >= 12]


def strip_duplicate_lead(text: str) -> tuple[str, bool]:
    """删掉与后文重复的章首导言。

    现象：第 10 章开头一段导言把 10.1~10.6 的内容先浓缩讲了一遍，评委会看到明显重复。
    判据只看机械重叠：导言里的句子有一半以上在后文逐字出现，就认定是重复导言。
    """
    body = str(text or "")
    if len(body) < 120:
        return body, False
    # 只处理「第一个小标题之前」的导言
    head_match = re.search(r"(?m)^[ \t]*(?:#{2,4}[ \t]*\S|\d+(?:\.\d+)+\s*\S)", body)
    if not head_match or head_match.start() < 60:
        return body, False
    lead = body[:head_match.start()].strip()
    rest = body[head_match.start():]
    if len(lead) < 60:
        return body, False
    lead_sentences = _sentences(lead)
    if not lead_sentences:
        return body, False
    flat_rest = re.sub(r"\s+", "", rest)
    repeated = sum(1 for s in lead_sentences if re.sub(r"\s+", "", s) in flat_rest)
    if repeated * 2 < len(lead_sentences):
        return body, False
    return rest.strip(), True


# ── 代理商身份表述：按招标原文条件抑制 ────────────────────────────────────────
# 实测事故（14.3.1）：「投标人为代理经销商时，同时附制造商相应资质材料」，而本项目
# 投标人须知前附表明确「不接受代理商投标」。这句话等于投标人自己声明「我是代理商」，
# 评委照这句就能判资格不符 —— 必须整句去掉或改写成制造商口径。
# 只有在**招标原文确实不接受代理商**时才动它：允许代理投标的项目里，代理商是合法主体，
# 句子必须原样保留（改写反而是错误陈述）。
_AGENT_REJECT = (
    re.compile(r"(?:不接受|不允许|不接收|不予接受|拒绝|禁止|不设)[^。；\n]{0,12}(?:代理|经销)"),
    re.compile(r"(?:代理|经销)[^。；\n]{0,8}(?:不接受|不允许|不得|不予)"),
)
# 「我方是代理商」这类句子：投标人/我方 + 为/是/如为 + 代理(商/经销商)
_AGENT_ASSUME = (
    re.compile(r"(?:投标人|供应商|投标单位|我方|本公司|我公司|申请人)[^。；\n]{0,8}"
               r"(?:为|是|系|如为|若为|属)[^。；\n]{0,8}(?:代理经销商|代理商|经销商|代理投标|代理机构)"),
    re.compile(r"(?:如|若|当)[^。；\n]{0,8}(?:代理经销商|代理商|经销商)[^。；\n]{0,14}"
               r"(?:投标|参加|参与|响应)"),
    re.compile(r"(?:代理经销商|代理商|经销商)[^。；\n]{0,10}(?:投标时|参加投标|参与投标|投标的)"),
)
_AGENT_REPLACEMENT = ("本项目不接受代理商投标，投标人为投标设备制造商，"
                      "直接提供制造商资质材料")
# 行首编号/项目符号（「14.3.1 」「（3）」「1.」「- 」）：改写整句时要保住，否则
# 正文编号会断档（编号归一在后面才跑，这里先自己保号）。
_LINE_MARKER = re.compile(
    r"^[ \t　]*(?:\d{1,2}(?:\.\d+)+\s*|\d{1,2}\s*[、.．)）]|[(（]\s*\d{1,2}\s*[)）]"
    r"|第[一二三四五六七八九十]{1,3}[条项]\s*|[-*•·]\s*)")
_SENTENCE_TAIL = "。；！？!?;"


def tender_rejects_agents(tender_text: str) -> bool:
    """招标原文是否明确「不接受代理商（投标）」。

    只看招标人自己的否决口径：不接受代理商/代理投标/经销商，或代理商不得参加。
    招标文件允许代理商投标时返回 False，正文里的代理表述必须原样保留。
    """
    text = str(tender_text or "")
    if not text.strip():
        return False
    flat = re.sub(r"\s+", "", text)
    return any(pattern.search(flat) for pattern in _AGENT_REJECT)


def _is_agent_rejection(sentence: str) -> bool:
    """这句本身就是「不接受代理商投标」的正确否定表述 —— 不要改写它。"""
    return any(pattern.search(str(sentence or "")) for pattern in _AGENT_REJECT)


def _rewrite_agency_sentence(part: str) -> str:
    """整句换成制造商口径，保留行首编号与原句的句末标点。"""
    marked = _LINE_MARKER.match(str(part or ""))
    prefix = part[:marked.end()] if marked else ""
    stripped = str(part or "").rstrip()
    tail = stripped[-1] if stripped and stripped[-1] in _SENTENCE_TAIL else ""
    return prefix + _AGENT_REPLACEMENT + (tail or "。")


def suppress_agency_assumptions(text: str, tender_text: str) -> tuple[str, list[str]]:
    """招标文件不接受代理商时，去掉正文里「投标人为代理商时…」这类自我声明。

    返回 (处理后正文, 处理记录)。判据全部来自招标原文，不写死任何项目：
    * 招标原文没有「不接受代理商/经销商」口径 → 原文原样返回（允许代理投标的项目，
      代理商是合法投标主体，改写反而是错误陈述）；
    * 有该口径 → 逐句找「投标人/我方为（如为/若为）代理商/经销商…」这类假设句，
      整句替换为「本项目不接受代理商投标，投标人为投标设备制造商，直接提供制造商
      资质材料」，保留行首编号与句末标点；
    * 已经是「不接受代理商投标」的否定句不动（避免把正确表述改成绕口的话，也保证
      重复调用幂等）；表格行不拆句，避免把表格切断。
    """
    body = str(text or "")
    if not body.strip() or not tender_rejects_agents(tender_text):
        return body, []
    hits = 0
    rebuilt_lines: list[str] = []
    for line in body.splitlines():
        if "|" in line:                     # 表格行不拆句：拆开会把表格切断
            rebuilt_lines.append(line)
            continue
        rebuilt: list[str] = []
        replaced = False                     # 同一行只留一处改写，避免同一句说两遍
        for part in re.split(r"(?<=[。；！？!?;])", line):
            core = part.strip()
            assumes = core and any(pattern.search(core) for pattern in _AGENT_ASSUME)
            if not assumes or _is_agent_rejection(core):
                rebuilt.append(part)
                continue
            if replaced:
                # 同一行里重复的代理假设句：整句丢弃，并把上一句留下的分号收成句号
                if rebuilt and rebuilt[-1].rstrip()[-1:] in ("；", ";"):
                    rebuilt[-1] = rebuilt[-1].rstrip()[:-1] + "。"
                continue
            rebuilt.append(_rewrite_agency_sentence(part))
            replaced = True
            hits += 1
        rebuilt_lines.append("".join(rebuilt))
    if not hits:
        return body, []
    return ("\n".join(rebuilt_lines),
            ["删除代理商身份表述（招标文件不接受代理商，改为制造商口径）"])


def looks_truncated(text: str) -> str:
    """章末是否明显没写完；没写完返回原因，写完返回空串。

    只判机械信号：最后一行既不是句末标点/表格行/落款，也不是数字单位结尾。
    """
    body = str(text or "").strip()
    if len(body) < 40:
        return "正文过短（不足 40 字），疑似未生成完整"
    tail = body.splitlines()[-1].strip()
    if not tail:
        return "正文结尾为空行，疑似未写完"
    if tail.startswith("|") or tail.endswith("|"):
        return ""                      # 表格行
    if re.search(r"(?:签字|签章|盖章|公章|年\s*月\s*日|日期[:：]).{0,20}$", tail):
        return ""                      # 落款
    if re.search(r"[。！？；：”』）】]$", tail):
        return ""
    if re.search(r"(?:\d+(?:\.\d+)?\s*(?:mm|m|kW|t/h|MPa|kg|年|天|个月|小时|%)|[一二三四五六七八九十]+(?:年|天|个月))$", tail):
        return ""                      # 以数值+单位收尾，算写完
    if len(tail) <= 6 and not re.search(r"[，、,]", tail):
        return "章末半句断尾：%s" % tail[:20]
    if not re.search(r"[。！？；：]", tail):
        return "章末半句断尾：%s" % tail[-24:]
    return ""


def hygienic_text(text: str) -> tuple[str, list[str]]:
    """按顺序做正文卫生处理；返回 (文本, 处理记录)。"""
    notes: list[str] = []
    body, changed = strip_prompt_leaks(text)
    if changed:
        notes.append("清除提示词泄漏")
    body, changed = strip_ellipsis_lines(body)
    if changed:
        notes.append("删除省略号占位行")
    body, changed = strip_page_numbers_near_signature(body)
    if changed:
        notes.append("清除落款页脚数字")
    body, furniture_notes = strip_page_furniture(body)
    notes.extend(furniture_notes)
    body, changed = fix_unit_gap(body)
    if changed:
        notes.append("量词空格归一")
    body, changed = strip_duplicate_lead(body)
    if changed:
        notes.append("删除重复导言")
    return body, notes