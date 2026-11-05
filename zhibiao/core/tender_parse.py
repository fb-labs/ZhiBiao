"""招标文件解析加固（P0）。

线上实测到的六类抽取错误（2026-10-17，项目「示例煤矿端头及超前支护技术研究与应用」）：

  1) **目录点线当正文**：`第五章 技术规格书 .................47`
     → `技术需求 = "书 .........................47"`（真正的技术规格书整章没进上下文）
  2) **空白标签当取值**：密封标记里的 `①招标人：` / `②招标代理机构：`
     → `招标人 = "②招标代理机构："`（取到了下一个标签）
  3) **合同格式模板的掩码当项目名称**：`项目名称： *********************`
     → 封面直接打印 `项目名称：*****`；真值在「投标人须知前附表」里（无冒号且换行）
  4) **正文病句被截断**：`5、投标方在中标后须保证项目在规定工期内完成施工`
     → `工期要求 = "内完成施工"`（真值「服务期：合同签订后90 日历天」）
  5) **姓名与电话各自抓、配对错乱**：`联系人=赵先生`（招标人，电话 0356-5828565）
     却配上 `联系电话=400-0351-097`（另一处的号码）
  6) **技术规格书未进入生成上下文** → 正文出现 200+ 处 `{{待填}}`

本模块只负责「定位与清洗」，不改动评分项抽取与 15 维深度解读。
"""

from __future__ import annotations

import bisect
import re
from typing import Any, Iterable

# ─────────────────────────── 一、去噪 ───────────────────────────

_LEADER_RUN = re.compile(r"[.．。·•‥…]{3,}")
_TOC_LINE = re.compile(r"[.．。·•‥…]{3,}\s*\d{0,4}\s*$")
_PAGE_ONLY = re.compile(r"^\s*(?:第\s*)?\d{1,4}\s*(?:页)?\s*$")
_DIGITS = re.compile(r"\d+")


def _norm_for_count(line: str) -> str:
    return _DIGITS.sub("#", line.strip())


def strip_tender_noise(text: str) -> str:
    """清掉目录点线行、页码行与反复出现的页眉页脚。

    实测页眉页脚形如：
        示例能源集团示例煤矿分公司端头及超前支护技术研究与应用
        山西兰花真诚招标代理有限公司 电话：0356－2295027 邮箱 ...@163.com 48
        招标文件
    它们每页重复，会把关键词附近的取值污染成"招标文件""48"之类。
    """
    lines = (text or "").splitlines()
    counts: dict[str, int] = {}
    for line in lines:
        key = _norm_for_count(line)
        if 2 <= len(key) <= 90:
            counts[key] = counts.get(key, 0) + 1
    kept: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if _PAGE_ONLY.match(s):
            continue
        if _TOC_LINE.search(s):
            continue
        key = _norm_for_count(s)
        if counts.get(key, 0) >= 3 and len(key) <= 90:
            # 重复出现的不都是页眉页脚：参数表行（`1 立柱 φ200 数量 4`）数字归一成 `#` 后同键，
            # 但它们是真实规格内容，命中单位/参数特征的一律保留。
            if not _is_param_line(s):
                continue
        kept.append(s)
    return "\n".join(kept)


# ─────────────────────── 二、取值清洗与可用性判定 ───────────────────────

_LEAD_JUNK = re.compile(
    r"^[\s:：\-—–~·、,，;；\]\[()（）【】<>《》\"'“”‘’①-⑳⒈-⒛]+"
)
_TAIL_JUNK = re.compile(
    r"[\s:：\-—–~·、,，;；\]\[()（）【】<>《》\"'“”‘’]+$"
)
_MASKED_ONLY = re.compile(r"^[\s*＊#＃xX×\-—_]+$")
# 封面/签章行尾的残片（实测「山西兰花真诚招标代理有限公司 （盖章」）
_SEAL_TAIL = re.compile(r"[（(]\s*(?:盖章|签章|签字|公章)[）)]?\s*$|[（(]\s*(?:盖章|签章|签字|公章)\s*$")
# 金额字段：整句里只取金额本身（实测「采购预算为 600 万元，投标报价不得超过…」）
_MONEY = re.compile(r"[\d,，.]+\s*(?:亿元|万元|元)")
# 时间字段必须像日期/时间（实测被抓成「起 90 天」）
_DATEISH = re.compile(r"\d{4}\s*年|\d{1,2}\s*[:：]\s*\d{2}")
# 平台客服/监督举报等号码不得当作招标人电话
_NOT_CONTACT_LINE = re.compile(r"客服|服务热线|平台|监督|举报|纪委|监察|技术支持|咨询热线|CA\s*咨询|数字证书|接收异议|提出异议", re.I)
# 引用不是事实：允许普通名称中出现「见」，但拒绝以引用动词开头的前附表/章节指针。
_PROXY_VALUE = re.compile(r"^(?:请)?(?:参见|详见|见|按照|按|以).{0,30}(?:前附表|附表|须知|章节|第.{1,8}章|公告|为准)")
# 正文句子（而非字段取值）的标记词
_NOT_A_VALUE = (
    "结束后所需", "所需用于", "用于运行", "另行", "详见", "见第", "按本章", "按本招标",
    "如上", "同上", "中标后须", "规定工期", "视为",
)
# 「项目名称」的定义句而不是取值：
# 线上事故（第二个标书v2）：封面项目名称打印成「即招标项目所属的工程建设项目，见投标人须知前」——
# 这是投标人须知 1.1.5 对「工程项目名称」的解释句，被当成 1.1.4 的取值抄到了封面。
_DEFINITION_VALUE = re.compile(
    r"^即|^是指|见投标人须知|见投标人须知前附表|所属的工程建设项目|^详见|^参见|^另见")
# 「买方名称」「买方联系人及电话」「投标人地址」这类只由字段名构成的取值，不是真实取值。
_LABEL_ONLY = re.compile(
    r"^(?:买方|卖方|甲方|乙方|招标人|投标人|采购人|供应商|收货人|项目)?"
    r"[\u4e00-\u9fa5]{0,8}(?:名称|联系人|联系电话|电话|地址|日期|编号)$")
_FIELD_LABELS = (
    "招标代理机构", "采购代理机构", "代理机构", "招标人", "采购人", "招标单位",
    "项目名称", "项目编号", "招标编号", "联系人", "联系电话", "联系方式",
    "服务期", "工期", "质保期", "保修期", "技术需求", "技术要求", "人员要求",
    "预算金额", "投标截止时间", "开标时间", "项目地点", "服务地点",
    "评分办法", "评标办法", "资质要求", "投标保证金", "投标文件", "招标文件",
    "前附表", "货物名称", "投标单位名称", "电子邮箱",
)


def flex(label: str) -> str:
    """把中文标签变成容忍空格的模式：「招 标 人」也能命中「招标人」。"""
    return r"\s*".join(re.escape(ch) for ch in label)


def _balance_brackets(v: str) -> str:
    """括号配对：单边括号（如「山西省·晋城市（招标人指定地点」）在封面上很难看，直接去掉多余的那个。"""
    for left, right in (("（", "）"), ("(", ")"), ("【", "】"), ("《", "》")):
        nl, nr = v.count(left), v.count(right)
        if nl == nr:
            continue
        if nl > nr:
            for _ in range(nl - nr):
                idx = v.rfind(left)
                if idx >= 0:
                    v = v[:idx] + v[idx + 1:]
        else:
            for _ in range(nr - nl):
                idx = v.find(right)
                if idx >= 0:
                    v = v[:idx] + v[idx + 1:]
    return v


# 负号保护：`-20℃` / `-5~5MPa` 的负号是数值的一部分，不能被 _LEAD_JUNK 当列表符号剥掉；
# `- 20`（负号后带空格）仍是列表符号，照旧剥离。
_NEG_HOLD = "\x00"


def _hold_negative_sign(v: str) -> str:
    return re.sub(r"^(\s*)([-－—–])(?=\d)", lambda m: m.group(1) + _NEG_HOLD, v)


def clean_value(value: Any) -> str:
    v = re.sub(r"\s+", " ", str(value or "")).strip()
    v = _LEADER_RUN.sub(" ", v)
    v = _SEAL_TAIL.sub("", v)
    v = _hold_negative_sign(v)
    v = _LEAD_JUNK.sub("", v)
    v = v.replace(_NEG_HOLD, "-")
    v = re.sub(r"^\s*(?:为|是)\s*", "", v)
    v = _TAIL_JUNK.sub("", v)
    v = _balance_brackets(v)
    return re.sub(r"\s{2,}", " ", v).strip()


def value_usable(name: str, value: str) -> bool:
    """判断一个取值是不是「真的取到了」——把残片、标签、掩码、页码判为不可用。"""
    v = clean_value(value)
    if len(v) < 2:
        return False
    if _MASKED_ONLY.match(v) or "****" in v or _PROXY_VALUE.search(v):
        return False
    if name == "项目编号" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/－—–\-]*", v):
        return False
    if _LEADER_RUN.search(str(value or "")):
        return False
    if _PAGE_ONLY.match(v):
        return False
    if v.endswith(("：", ":")):
        return False
    # 值里混进了另一个字段标签 → 明显抓到下一行了
    for lab in _FIELD_LABELS:
        if lab and lab not in name and v.startswith(lab) and len(v) <= 24:
            return False
    # 目录式引用（「书 47」这种）本身就是残片
    if re.fullmatch(r"[^\d]{0,6}\d{1,4}", v):
        return False
    need = {
        "工期要求": r"(?:天|日|月|年|周|工期)",
        "质保期": r"(?:月|年|天|日)",
        "联系电话": r"\d{7,}",
        "招标人联系电话": r"\d{7,}",
        "招标代理联系电话": r"\d{7,}",
        "预算金额": r"(?:元|万元|亿元)",
        "投标保证金": r"(?:元|万元|亿元|%)",
        "项目编号": r"[A-Za-z0-9]",
        "投标截止时间": r"(?:年|月|日|时|[:：])",
        "开标时间": r"(?:年|月|日|时|[:：])",
    }.get(name)
    if need and not re.search(need, v):
        return False
    # 明显是"正文句子"而不是"字段取值"的，判为不可用。
    # 实测：质保期被抓成「质保期结束后两年内所需用于运行、」（合同条款里的整句），
    # 真值是「整机质保 12 个月、易损件 18 个月」。
    if any(mark in v for mark in _NOT_A_VALUE):
        return False
    max_len = {
        "项目名称": 80, "项目编号": 40, "招标人": 40, "招标代理机构": 40,
        "工期要求": 40, "质保期": 24, "项目地点": 40, "预算金额": 24,
        "招标人联系人": 20, "招标人联系电话": 24,
        "招标代理联系人": 20, "招标代理联系电话": 24,
    }.get(name)
    if max_len and len(v) > max_len:
        return False
    date_need = {
        "投标截止时间": _DATEISH, "开标时间": _DATEISH,
    }.get(name)
    if date_need is not None and not date_need.search(v):
        return False
    if name == "技术需求" and _looks_like_heading(v):
        # 章节小标题（`一）支架基本参数`）不是技术需求内容，不能算已确认事实
        return False
    if name == "项目名称" and len(v) < 4:
        return False
    # 定义句/引用句不是项目名称：「即招标项目所属的工程建设项目，见投标人须知前」
    if name in ("项目名称", "招标项目名称") and _DEFINITION_VALUE.search(v):
        return False
    # 取值本身就是别的字段名/表头列名（「买方名称」「买方联系人及电话」）：
    # 第四章合同格式的表格表头恰好含「项目名称」列，抽到的下一个词是列名而不是项目名。
    if name in ("项目名称", "招标项目名称", "招标人", "项目地点", "招标编号") \
            and not re.search(r"\d", v) and _LABEL_ONLY.match(v):
        return False
    if name in ("招标人", "招标代理机构") and "代理" in v and name == "招标人":
        return False
    return True


# ──────────────── 二点五、项目名/标题的定界与清洗（唯一实现） ────────────────
# 封面标题、页眉、目录与导出 docx 标题、解读关键事实与项目列表都调这一个函数，
# 避免三处各写一套正则、同一个名字在不同界面不一样。
#
# 线上实测（v7 封面第一行）：
#   1.1.4 招标项目名称
#   阳泉煤业集团物资经销有限责任公司（2025）采掘设备（第三批）新景综采皮带机
#   1.2.1 资金来源及比例 资金来源为股份自有资金，出资比例为 100%
# 项目名被拼成「…新景综采皮带机1.2.1 资金来源及比例 资金来源为股份自有资金，出资比例为 100%」。
# 成因：`_join_continuation` 只把 `^\d{1,3}\s` 当「新的一行」，带点条款号 `1.2.1` 被当成
# 折行续写接进了取值。因此定界规则与续行判定共用下面这一套小节编号正则。

PROJECT_NAME_MAX_LEN = 80

# 小节编号开头：`1.2.1`（≥2 个点）、`2.1`（单点，须后接空白/冒号/顿号或行尾）、
# `（三）`（数字紧贴右括号，所以「（第三批）」不会被误判）、`三、`。
_SECTION_DOTTED = re.compile(r"(?<![\d.])\d{1,3}(?:[.．]\d{1,3}){2,}")
_SECTION_SINGLE = re.compile(r"(?<![\d.])\d{1,3}[.．]\d{1,3}(?=[\s:：、]|$)")
_SECTION_PAREN = re.compile(r"[（(]\s*[一二三四五六七八九十]{1,3}\s*[）)]")
_SECTION_ORDINAL = re.compile(r"[一二三四五六七八九十]{1,3}\s*、")
# 句末标点：项目名不可能跨句
_SENTENCE_END = re.compile(r"[。；;]")

# 前附表字段名：出现即说明后面是另一个字段（或它的值），不再是项目名本身。
_PROJECT_HEAD_FIELDS: tuple[str, ...] = (
    "资金来源", "资金比例", "出资比例", "招标范围", "采购范围", "招标内容", "采购内容",
    "招标方式", "采购方式", "计划工期", "工期要求", "服务期限", "交货期", "交货地点",
    "质量要求", "质量标准", "投标人资格", "资格要求", "资质要求", "评标办法", "评分办法",
    "评审办法", "投标有效期", "投标保证金", "投标担保", "最高限价", "招标控制价",
    "预算金额", "项目地点", "服务地点", "建设地点", "项目编号", "招标编号", "采购编号",
    "招标人", "采购人", "招标代理机构", "代理机构", "项目概况", "项目规模",
    "标段划分", "建设规模",
)
_PROJECT_HEAD_FIELD = re.compile("|".join(flex(x) for x in _PROJECT_HEAD_FIELDS))
_PROJECT_CUTTERS = (_SECTION_DOTTED, _SECTION_SINGLE, _SECTION_PAREN,
                    _SECTION_ORDINAL, _PROJECT_HEAD_FIELD, _SENTENCE_END)

# 只剥列表符号/冒号，**不剥括号**：`（第三批）` 是项目名的一部分，而 clean_value 的
# _TAIL_JUNK/_LEAD_JUNK 会把首尾括号当垃圾剥掉（「…采掘设备第三批」），因此这里不复用。
_PROJECT_LEAD_JUNK = re.compile(r"^[\s:：\-—–~·、,，;；.．*#]+")
_PROJECT_TAIL_JUNK = re.compile(r"[\s:：\-—–~·、,，;；.．]+$")
# 尾部悬挂的编号：`…皮带机 1.2.1` / `…皮带机2.1.` / `…皮带机（三）` / `…皮带机 3`。
# 末条要求有分隔符，避免把「四标段2」这种名字里的业务数字当尾号删掉。
_PROJECT_DANGLING = (
    re.compile(r"(?<!\d)\d{1,3}(?:[.．]\d{1,3})+[.．、]?\s*$"),
    re.compile(r"\s\d{1,3}[.．]\s*$"),
    re.compile(r"[（(]\s*[一二三四五六七八九十]{1,3}\s*[）)]\s*$"),
    re.compile(r"\s\d{1,3}\s*$"),
)


def is_section_start(text: str) -> bool:
    """这一行是不是「新的小节/字段行」而不是上一行的折行续写。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    if re.match(r"^\d{1,3}\s", raw):              # 「3 项目编号 …」
        return True
    return any(p.match(raw) for p in
               (_SECTION_DOTTED, _SECTION_SINGLE, _SECTION_PAREN, _SECTION_ORDINAL,
                _PROJECT_HEAD_FIELD))


def _project_name_cut(value: str) -> int:
    """第一个不属于项目名的位置（小节编号/字段名/句末标点）；整串都是名字时返回 len。"""
    cut = len(value)
    for pattern in _PROJECT_CUTTERS:
        found = pattern.search(value)
        if found is not None and found.start() < cut:
            cut = found.start()
    return cut


def _strip_project_tail(value: str) -> str:
    """去掉尾部悬挂的空白、分隔符与编号。"""
    v = _PROJECT_TAIL_JUNK.sub("", str(value or "")).strip()
    changed = True
    while changed and v:
        changed = False
        for pattern in _PROJECT_DANGLING:
            new = _PROJECT_TAIL_JUNK.sub("", pattern.sub("", v)).strip()
            if new != v:
                v, changed = new, True
    return v


def clean_project_name(value: Any, *, max_len: int = PROJECT_NAME_MAX_LEN) -> str:
    """把「项目名」定界成一行，供所有出口共用（幂等：干净的名字再洗一次不变）。

    规则：
      1) 遇到小节编号开头（`1.2.1`、`2.1`、`（三）`、`三、`）即截断；
      2) 遇到前附表字段名（`资金来源`、`出资比例`、`招标范围`、`计划工期`…）即截断；
      3) 遇 `。`/`；` 即截断，并限制单个项目名长度（默认 80 字）；
      4) 去尾部悬挂编号与空白，但保留名字里合法的小节编号（`（第三批）`）。
    """
    v = re.sub(r"\s+", " ", str(value or "")).strip()
    if not v:
        return ""
    v = _LEADER_RUN.sub(" ", v)
    cut = _project_name_cut(v)
    if cut <= 0:
        # 整串以小节编号/字段名开头：这是下一行（或整行的字段行），不是项目名。
        return ""
    v = _PROJECT_LEAD_JUNK.sub("", v[:cut])
    v = _balance_brackets(v)
    v = _strip_project_tail(v)
    if len(v) > max_len:
        v = _balance_brackets(_strip_project_tail(v[:max_len]))
        v = _strip_project_tail(v)
    return re.sub(r"\s{2,}", " ", v).strip()


# ─────────────────────── 三、按标签抽取（含前附表行） ───────────────────────

# 前附表行：「1.1.4 招标项目名称 阳泉煤业…」这种带条款号的行，条款号是 1.1.4 而不是 1，
# 旧正则只认 `^\d{1,3}\s`，导致整行被跳过、退回下一个命中「项目名称」的表格表头，
# 封面上就出现了「买方名称买方联系人及电话」（第四章合同模板的列名）。
_ROW_TABLE = re.compile(r"^\s*\d{1,3}(?:[.．]\d{1,3})*[.．]?\s+\S")
_COLON = ("：", ":")


def _join_continuation(value: str, following: list[str]) -> str:
    """前附表里长取值会折行（「…端头及超前支护技术研究」/「与应用项目」），需要接上下一行。

    只接一行、且下一行不能是字段标签或新的表格行、当前取值也不能已经以句号收尾。
    """
    if not value or not following:
        return value
    raw = str(following[0] or "").strip(" 　\t")
    nxt = clean_value(raw)
    if not nxt or len(value) >= 60:
        return value
    if any(nxt.startswith(x) for x in _FIELD_LABELS):
        return value
    # 下一行是**新的字段行**（`1.2.1 资金来源及比例 资金来源为…`、`三、计划工期 90 天`），
    # 不是折行续写。旧检查只认 `^\d{1,3}\s`，带点条款号被当成续行拼进了取值（v7 封面实测）。
    if is_section_start(raw):
        return value
    if value.endswith(("。", "；", ";", "！", "!")):
        return value
    # 接续行用原文而不是 clean_value 的结果：`（第三批）新景综采皮带机` 的开头括号
    # 会被 _LEAD_JUNK 当「开头的括注垃圾」整段剥掉，项目名称就成了「采掘设备第三批…」。
    return value + raw.lstrip()


def _candidate_from_line(line: str, start: int, end: int, next_lines: list[str]) -> str:
    tail = line[end:].lstrip(" 　")
    if tail[:1] in _COLON:                       # 「服务期：合同签订后90 日历天」
        value = clean_value(tail[1:])
        if value:
            return value
        for nxt in next_lines[:2]:               # 冒号后换行
            cand = clean_value(nxt)
            if not cand or cand.endswith(_COLON):
                continue
            # 空标签（`招 标 人：` 下一行就是 `项目名称：…`）不能把下一个字段标签当取值：
            # 与 extract_labeled 的跨行分支同口径，以任一 _FIELD_LABELS 开头即拒绝。
            if any(cand.startswith(x) for x in _FIELD_LABELS):
                continue
            return cand
        return ""
    if _ROW_TABLE.match(line):                   # 前附表行「5 服务地点 招标人指定地点」
        # 先接续行再归一：折行处常把「（第三批）」拆成「（第」/「三批）」，
        # 先 clean 会把没配对的半括号删掉，项目名称就变成「采掘设备第三批…」。
        return clean_value(_join_continuation(tail.strip(" 　"), next_lines))
    # 正文里的「整机质保 12 个月，易损件质保 18 个月」这类：标签后紧跟数字就是取值，
    # 但句子里的「…保证项目在规定工期内完成施工」不会命中（后面不是数字）。
    if re.match(r"^\s*(?:为|是|满|不低于|不少于|约|≥)?\s*\d", tail) and len(tail) <= 60:
        val = re.split(r"[，,；;]", tail)[0]
        # 「质保期满 1 年」→「1 年」：满/为/是 是连接词，不是取值的一部分
        val = re.sub(r"^\s*(?:为|是|满)\s*(?=\d)", "", val)
        return clean_value(val)
    return ""


def extract_labeled(
    noise: str,
    labels: Iterable[str],
    *,
    max_len: int = 220,
    cross_line: bool = True,
    field: str = "",
) -> str:
    """按标签优先级抽取取值：先试高优先级标签，再按文档顺序取第一个可用值。

    `field` 用于字段级校验（例如「工期要求」必须有天/月/年），不传则用标签名兜底。
    """
    lines = [x for x in (noise or "").splitlines()]
    for label in labels:
        pat = re.compile(flex(label))
        for idx, line in enumerate(lines):
            m = pat.search(line)
            if not m:
                continue
            nxt = [x for x in lines[idx + 1: idx + 3] if x.strip()]
            value = _candidate_from_line(line, m.start(), m.end(), nxt)
            if not value and cross_line and idx + 1 < len(lines):
                after = line[m.end():].strip(" 　")
                if not after or after in _COLON:  # 标签独占一行，取下一行
                    cand = clean_value(lines[idx + 1])
                    if cand and not any(cand.startswith(x) for x in _FIELD_LABELS):
                        # 前附表换行（项目名称占两行）也要接上
                        value = _join_continuation(cand, [x for x in lines[idx + 2: idx + 3] if x.strip()])
            key = field or label
            if key == "项目编号" or label in ("招标项目编号", "招标编号", "采购编号", "项目编码"):
                # 公告内嵌编号之后常紧接「），项目资金来源…」；只取编号 token。
                token = re.match(r"[A-Za-z0-9][A-Za-z0-9._/－—–\-]*", value)
                value = token.group(0) if token else ""
            if value and len(value) > max_len:
                value = ""
            if value and not value_usable(key, value):
                value = ""
            if value and key in ("预算金额", "投标保证金"):
                money = _MONEY.search(value)
                value = money.group(0).strip() if money else ""
            if value:
                return value
    return ""


# ────────────── 三之二、招标/采购编号（专用抽取器） ──────────────
# 线上实测（新3，山西某矿 2026 年度中小修理四标段，48 页询比采购文件）：
#   封面只写 `采购编号：HP260901-049`，全篇一次「招标编号」都没有；
#   FIELD_LABELS["招标编号"] 只认 招标编号/招标文件编号/采购文件编号，
#   于是「招标文件体检 · 解析质量」把招标编号判成「未抽取到」，封面要素 4/5。
#
# 编号为什么单独成器、不和项目名称/工期共用一条取值链：
#   1) 别名多，且常写成「采购编号」「招标编码」「标段编号」「文件编号」；
#   2) 排版上常与标签被换行拆开、写在括号里、冒号前后带空格或全角冒号；
#   3) 形态可校验（字母数字与 -－—_/ 组成、6~40 字、至少 2 个数字），
#      据此把「第 32 页」「3.1.1」「2026年1月」这类相邻文本硬挡掉；
#   4) 同一编号会在封面、前附表、公告里重复出现，需要按「位置 + 次数」定夺。
#
# 两级别名：先认「招标/采购编号」本体，再退回「项目编号」类。两套号并存时
# （招标编号 JSH… 与 招标项目编号 M1401… 同时出现）绝不能让项目编号顶替招标编号。
_TENDER_NO_ALIASES: tuple[tuple[str, ...], ...] = (
    ("招标编号", "招标文件编号", "采购文件编号", "采购编号", "招标编码",
     "标段编号", "文件编号", "采购项目编号", "项目招标编号"),
    ("招标项目编号", "项目编号", "项目编码", "招标项目编码", "标段项目编号"),
)

# 编号本体：字母数字与 -－—_/ ，6~40 字（不含空格：表格行里「编号 12 项目名」不能被粘成一个号）。
# 末尾的负向断言保证「超长串」整体不被取：否则 46 字的长串会被截成前 40 字当成编号。
_NO_BODY = r"[A-Za-z0-9\-－—–_/]{6,40}(?![A-Za-z0-9\-－—–_/])"
# 标签与编号之间允许：空格/全角空格/冒号/破折号，且允许**换行**（标签一行、编号下一行）
_NO_SEP = r"[ \t\u3000:：\-—–]{0,10}(?:\n[ \t\u3000:：\-—–]{0,10}){0,2}"
_NO_OPEN_BRACKET = r"(?:[（(【\[][ \t\u3000:：\-—–]{0,10})?"
_NO_CHARSET = re.compile(r"^[A-Za-z0-9\-－—–_/]+$")
# 日期不是编号（`2026-1`、`2026/01`）；页码不是编号
_NO_DATE = re.compile(r"^(?:19|20)\d{2}[-－—–_/]\d{1,2}(?:[-－—–_/]\d{1,2})?$")
_NO_PAGE = re.compile(r"^第?\d{1,4}页?$")
# 抽不到时给人工看的「像编号的串」；纯数字短串多半是邮编/电话/页码，诊断里降噪
_LOOSE_NO = re.compile(r"[A-Za-z0-9\-－—–_/]{6,40}(?![A-Za-z0-9\-－—–_/])")
_TENDER_NO_LIMIT = 40
# 页码估算：文本里没有分页标记时按 32 行/页近似，只用于「封面前 3 页优先」的排序
_LINES_PER_PAGE = 32
_PAGE_MARK = re.compile(
    r"^(?:={3,}[ \t]*)?(?:PAGE|第)[ \t]*(\d{1,4})[ \t]*(?:页)?[ \t]*(?:={3,})?$", re.I)


def _no_pattern(alias: str) -> "re.Pattern[str]":
    """一个别名的取值模式：标签 + 可选冒号/空格/换行/左括号 + 编号本体。"""
    return re.compile(flex(alias) + _NO_SEP + _NO_OPEN_BRACKET + "(" + _NO_BODY + ")")


_TENDER_NO_PATTERNS: tuple[tuple[int, int, str, "re.Pattern[str]"], ...] = tuple(
    (level, rank, alias, _no_pattern(alias))
    for level, group in enumerate(_TENDER_NO_ALIASES)
    for rank, alias in enumerate(group)
)


def _clean_no_token(raw: Any) -> str:
    """去掉编号两侧的空格与连接符（不动中间的字面）。"""
    return str(raw or "").strip(" \t\u3000-－—–_/")


def looks_like_tender_no(value: Any) -> bool:
    """编号形态校验：6~40 字、字母数字与 -－—_/ 、至少 2 个数字，且不是日期/页码。

    这样 `第 32 页`（含中文）、`3.1.1`（含点）、`2026年1月`（含中文）、
    `2026-1`（日期）都过不了；20 位数字编号（M1401…）能过。
    """
    v = _clean_no_token(re.sub(r"[ \t\u3000]+", "", str(value or "")))
    if not 6 <= len(v) <= _TENDER_NO_LIMIT:
        return False
    if not _NO_CHARSET.match(v):
        return False
    if sum(1 for ch in v if ch.isdigit()) < 2:
        return False
    if _NO_DATE.match(v) or _NO_PAGE.match(v):
        return False
    return True


def _line_pages(text: str) -> list[int]:
    """每一行所属的页码（1 起）。优先用 \\f 与 `===== PAGE n =====` 标记，都没有就按行估算。"""
    lines = text.splitlines()
    if "\f" in text:
        pages: list[int] = []
        page = 1
        for line in lines:
            pages.append(page)
            page += line.count("\f")
        return pages
    marks: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        found = _PAGE_MARK.match(line.strip())
        if not found:
            continue
        try:
            marks.append((index, int(found.group(1))))
        except ValueError:                    # 页码不是数字就当没写
            continue
    if marks:
        pages = []
        current, cursor = 1, 0
        for index in range(len(lines)):
            while cursor < len(marks) and marks[cursor][0] <= index:
                current = marks[cursor][1]
                cursor += 1
            pages.append(current)
        return pages
    return [1 + index // _LINES_PER_PAGE for index in range(len(lines))]


def _no_tier(page: int) -> int:
    """封面前 3 页 > 正文首 5 页（第 4~8 页）> 其余。"""
    if page <= 3:
        return 0
    if page <= 8:
        return 1
    return 2


def _loose_no_candidates(text: str, limit: int = 8) -> list[str]:
    """原文里「像编号」的候选串，供抽不到时人工一眼确认（不参与取值）。"""
    out: list[str] = []
    seen: set[str] = set()
    for match in _LOOSE_NO.finditer(str(text or "")):
        value = _clean_no_token(match.group(0))
        if value in seen or not looks_like_tender_no(value):
            continue
        if value.isdigit() and len(value) < 8:      # 邮编/电话/页码，不像编号
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def extract_tender_no(text: str) -> dict[str, Any]:
    """抽招标/采购编号（纯函数、幂等、不抛异常、抽不到不编造）。

    返回 ``{"value", "label", "page", "others", "candidates"}``：

    * ``value`` —— 选中的编号，抽不到时为空串（**不猜、不拼**）；
    * ``label`` —— 命中的别名（招标编号/采购编号/项目编号…），供界面说明来源；
    * ``page`` —— 该编号第一次出现的页码；
    * ``others`` —— 其余不同的编号（「其他候选编号」）；
    * ``candidates`` —— 抽不到时原文里像编号的串，供人工确认。

    取值优先级：封面前 3 页 > 正文首 5 页 > 其余；同一级里
    「招标/采购编号」本体 > 「项目编号」类；同一别名下不同编号取出现次数最多者，
    并列取最靠前；同一编号重复出现取第一次出现的位置。
    """
    blank = {"value": "", "label": "", "page": 0, "others": [], "candidates": []}
    try:
        source = str(text or "")
        if not source.strip():
            return blank
        line_pages = _line_pages(source)
        offsets = [0]
        for index, char in enumerate(source):
            if char == "\n":
                offsets.append(index + 1)
        # 先按位置排序再去重叠：`招标项目编号` 里含 `项目编号`，同一次命中不能算两次
        matches: list[tuple[int, int, int, int, str, str]] = []
        for level, rank, alias, pattern in _TENDER_NO_PATTERNS:
            for match in pattern.finditer(source):
                matches.append((match.start(1), match.end(1), level, rank, alias,
                                match.group(1)))
        matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        found: dict[str, dict[str, Any]] = {}
        last_end = -1
        for start, end, level, rank, alias, raw in matches:
            if start < last_end:
                continue
            value = _clean_no_token(raw)
            if not looks_like_tender_no(value):
                last_end = end
                continue
            last_end = end
            line = bisect.bisect_right(offsets, start) - 1
            page = line_pages[line] if 0 <= line < len(line_pages) else 1
            info = found.get(value)
            if info is None:
                found[value] = {"value": value, "label": alias, "page": page,
                                "tier": _no_tier(page), "rank": (level, rank),
                                "count": 1, "first": start}
                continue
            info["count"] += 1
            if (level, rank) < info["rank"]:
                info["rank"], info["label"] = (level, rank), alias
            if page < info["page"]:
                info["page"], info["tier"] = page, _no_tier(page)
            info["first"] = min(info["first"], start)
        if not found:
            return dict(blank, candidates=_loose_no_candidates(source))
        ordered = sorted(found.values(),
                         key=lambda item: (item["tier"], item["rank"],
                                           -item["count"], item["first"]))
        best = ordered[0]
        return {"value": str(best["value"]), "label": str(best["label"]),
                "page": int(best["page"]),
                "others": [str(item["value"]) for item in ordered[1:]],
                "candidates": []}
    except Exception:  # noqa: BLE001 - 解析兜底不允许把整条链路带崩
        return blank


def _extract_project_name(text: str, noise: str) -> str:
    """保留 PDF 表格列来源：跨行居中的标签可能落在值的两行之间。"""
    lines = [line for line in text.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        match = re.search(flex("招标项目名称") + "|" + flex("项目名称"), line)
        if not match or not re.match(r"^\s*\d+(?:\.\d+)*\s+", line):
            continue
        # 只在左侧条款号+标签、右侧空值的表格行回看，不跨正文随意拼接。
        if line[match.end():].strip() or i == 0 or i + 1 >= len(lines):
            continue
        prev, nxt = lines[i - 1], lines[i + 1]
        indent = len(prev) - len(prev.lstrip())
        next_indent = len(nxt) - len(nxt.lstrip())
        if indent < match.end() or next_indent < match.end():
            continue
        if re.match(r"\d+(?:\.\d+)*\s", prev.strip()) or any(
            lab in prev for lab in _FIELD_LABELS
        ) or re.search(r"[:：]", prev):
            continue
        value = clean_value(prev.strip() + nxt.strip())
        if value_usable("项目名称", value):
            return value
    return extract_labeled(noise, FIELD_LABELS["项目名称"], field="项目名称")


# ─────────────────────── 四、联系人按角色配对 ───────────────────────

_BUYER = re.compile(r"(?:^|[^\u4e00-\u9fa5])(?:招\s*标\s*人|采\s*购\s*人|招\s*标\s*单\s*位|采\s*购\s*单\s*位|业\s*主)")
_AGENCY = re.compile(r"代\s*理\s*机\s*构|采\s*购\s*代\s*理|招\s*标\s*代\s*理")
_PERSON = re.compile(r"(?:联\s*系\s*人|联系人)\s*[:：]?\s*([^\s，。；:：\d]{2,12})")
_PHONE = re.compile(r"(?:电\s*话|联系电话|联系方式|手\s*机)\s*[:：]?\s*([\d\-—－\s]{7,20})")


def _clean_phone(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip("-—－")


def extract_contacts(noise: str) -> dict[str, str]:
    """按「角色 → 姓名/电话」成对抽取，避免姓名与电话张冠李戴。

    实测原文（九、联系方式）：
        招 标 人：示例能源集团股份有限公司示例煤矿分公司
        联系人：赵先生
        电话：0356-5828565
        招标代理机构：山西兰花真诚招标代理有限公司
        联 系 人：晋女士、常女士
        电 话： 0356-2295027
    旧逻辑把「联系人」与「联系电话」各自全库搜第一个，于是赵先生被配上了别处的 400 号码。
    """
    out: dict[str, str] = {
        "招标人联系人": "", "招标人联系电话": "",
        "招标代理联系人": "", "招标代理联系电话": "",
    }
    # 只有角色字段/表格行可以建立来源，不让「招标人为…」「招标人应…」正文
    # 把角色状态泄漏到几十行之外的 CA、平台或监督部门电话。
    header = re.compile(
        r"^\s*(?:\d+(?:\.\d+)*\s*[、.．]?\s*)?"
        r"(?P<role>招\s*标\s*代\s*理\s*机\s*构|采\s*购\s*代\s*理\s*机\s*构|"
        r"招\s*标\s*人|采\s*购\s*人|招\s*标\s*单\s*位|采\s*购\s*单\s*位|业\s*主)"
        r"(?=\s*[:：]|\s+|$)"
    )
    role = ""
    block: dict[str, str] = {}
    scores = {"buyer": -1, "agency": -1}
    age = 0

    def commit() -> None:
        if not role or not block:
            return
        # 同一个来源块中的姓名+电话高于孤立号码，整块替换，绝不跨块拼对。
        score = 2 * bool(block.get("person")) + bool(block.get("phone"))
        if score <= scores[role]:
            return
        scores[role] = score
        prefix = "招标人" if role == "buyer" else "招标代理"
        out[prefix + "联系人"] = block.get("person", "")
        out[prefix + "联系电话"] = block.get("phone", "")

    for line in (noise or "").splitlines():
        if not line.strip():
            continue
        m = header.search(line)
        if _NOT_CONTACT_LINE.search(line):
            commit()
            role, block = "", {}
            continue
        if m:
            commit()
            role = "agency" if "代理" in re.sub(r"\s+", "", m.group("role")) else "buyer"
            block, age = {}, 0
        elif role:
            age += 1
            # 新条款、章节、其他字段来源和过远文本均结束当前来源块。
            if age > 8 or re.match(r"^\s*(?:第.{1,8}章|\d+(?:\.\d+)*\s*[、.．]|\d+(?:\.\d+)+\s|开户|账号|名\s*称\s*[:：])", line):
                commit()
                role, block = "", {}
        if not role:
            continue
        person = _PERSON.search(line)
        if person and not block.get("person"):
            name = clean_value(person.group(1))
            if name and name not in ("无", "待定"):
                block["person"] = name
        phone = _PHONE.search(line)
        if phone and not block.get("phone"):
            block["phone"] = _clean_phone(phone.group(1))
    commit()
    return out


# ─────────────────────── 五、技术规格书要点 ───────────────────────

# 只认「章节名」级别的标题，避免把正文里的「技术要求」当成规格书章节起点
_SPEC_HEADS = ("技术规格书", "技术规格要求", "技术需求书", "供货要求")
_UNIT = re.compile(
    r"(?:kN|KN|MPa|mm|MM|GB\s*/?\s*T|NB\s*/?\s*T|MT\s*/?\s*T|AQ\s*\d|kg|kW|℃|%|"
    r"毫米|吨|米|台|套|组|架|个月|日历天|小时|分钟|kV|V)"
)
# 入选摘录用的「严格单位集」：多字或明确的工程单位。单字歧义单位（台/组/米/年/天/日/月）
# 会让「登陆金蝉平台」「实施小组」「烟台地区」混进摘录，因此只在紧邻数字时才认（见 _UNIT_AMBIG）。
_UNIT_STRICT = re.compile(
    r"(?:kN|KN|MPa|mm|MM|GB\s*/?\s*T|NB\s*/?\s*T|MT\s*/?\s*T|AQ\s*\d|kg|KG|kW|KW|℃|%|"
    r"Φ|φ|毫米|吨|台套|套|日历天|个月|小时|分钟|kV|KV|V|㎡|m³|m2|m3)"
)
_UNIT_AMBIG = re.compile(r"\d\s*(?:台|组|米|年|天|日|月|架|套)")
_SPEC_KEYWORD = re.compile(
    r"(?:支架|立柱|千斤顶|安全阀|操纵阀|阀组|液压|乳化液|钢板|材质|初撑力|工作阻力|"
    r"推移|顶梁|底座|掩护|喷雾|密封|镀层|焊接|探伤|试验|检验|标准|规程|规范|"
    r"型号|规格|数量|技术要求|性能|参数)"
)
_STANDARD_NO = re.compile(r"(?:GB|NB|MT|AQ)\s*/?\s*T?\s*\d")


def _is_param_line(line: str) -> bool:
    """参数/标准型行：数字归一成 `#` 后仍能靠单位与规格关键词认出（供去噪时豁免重复计数）。"""
    return bool(_UNIT.search(line) or _SPEC_KEYWORD.search(line) or _STANDARD_NO.search(line))


def extract_spec_digest(text: str, max_chars: int = 5200) -> str:
    """从「技术规格书」正文里抽出参数型要点，供正文生成引用。

    旧实现完全没有这段内容，导致正文只能写 `{{待填:具体数值}}`（实测 200+ 处）。
    """
    noise = strip_tender_noise(text)
    starts: list[int] = []
    for head in _SPEC_HEADS:
        starts += [m.start() for m in re.finditer(flex(head), noise)]
    if not starts:
        # 兜底：按语义找供货/技术要求章（不是每份文件都叫「第五章」）
        try:
            from zhibiao.core.tender_checkup import tech_chapter
            span = tech_chapter(text)
        except Exception:  # noqa: BLE001 - 解析兜底不允许把整条链路带崩
            span = None
        if span is not None:
            starts = [m.start() for m in re.finditer(flex(span["label"]), noise)]
    if not starts:
        return ""
    # 同名标题可能在目录、正文、附件里各出现一次：取「后文参数最密集」的那一处
    def _density(pos: int) -> int:
        window = noise[pos: pos + 1200]
        return (len(_SPEC_KEYWORD.findall(window)) + len(_UNIT.findall(window))
                + 2 * len(re.findall(r"(?:GB|NB|MT|AQ)\s*/?\s*T?\s*\d", window)))
    best = max(starts, key=_density)
    seg = noise[best: best + 40000]
    keep: list[str] = []
    total = 0
    for line in seg.splitlines():
        s = line.strip()
        if len(s) < 3 or _PAGE_ONLY.match(s) or _LEADER_RUN.search(s):
            continue
        # 只保留参数型/标准型行；「登陆金蝉平台(网址)」「实施小组」「烟台地区」这类句子不再入选
        hit = bool(_UNIT_STRICT.search(s) or _SPEC_KEYWORD.search(s)
                   or _UNIT_AMBIG.search(s) or _STANDARD_NO.search(s))
        if not hit:
            continue
        keep.append(s)
        total += len(s) + 1
        if total >= max_chars:
            break
    return "\n".join(keep)[:max_chars]


# ─────────────────────── 六、字段级修正表 ───────────────────────

FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "项目名称": ("招标项目名称", "项目名称"),
    "项目编号": ("招标项目编号", "项目编号", "项目编码"),
    # 招标编号与项目编号是两套号：封面/第一章里「招标编号：JSH2026-375」与「招标项目编号：
    # M1401000156004970001」并存，封面只打一个就会被质疑编号不全。
    # 别名按实际标书的写法补齐（询比采购文件常只写「采购编号」，实测事故就是这里漏的）；
    # 取值仍由 extract_tender_no 统一决定，这里只是让通用链路也不至于两眼一抹黑。
    "招标编号": ("招标编号", "招标文件编号", "采购文件编号", "采购编号", "招标编码",
                "标段编号", "文件编号", "采购项目编号", "项目招标编号"),
    "招标人": ("招标人", "采购人", "招标单位", "采购单位"),
    "招标代理机构": ("招标代理机构", "采购代理机构"),
    "预算金额": ("预算金额", "采购预算", "最高限价", "招标控制价"),
    "工期要求": ("服务期", "履约期限", "交货期", "完成时间", "工期"),
    "项目地点": ("服务地点", "项目地点", "交货地点", "实施地点", "交付地点"),
    "质保期": ("质保期", "保修期", "质量保证期", "免费维护期", "维保期", "质保", "保修"),
    "技术需求": ("技术需求", "技术要求", "采购需求", "技术规格"),
    "投标截止时间": ("投标截止时间", "递交截止时间", "响应文件递交截止时间", "报价截止时间"),
    "开标时间": ("开标时间", "开启时间", "唱标时间"),
    "投标保证金": ("投标保证金", "投标担保", "保证金"),
    "投标有效期": ("投标有效期", "报价有效期"),
    "付款方式": ("付款方式", "付款条件", "结算方式", "支付方式"),
    "验收标准": ("验收标准", "验收方式", "验收要求"),
    "资质要求": ("资质要求", "资格要求", "投标人资格"),
    "评分办法": ("评标办法", "评分办法", "评审办法", "评标方法"),
    "人员要求": ("人员要求", "项目负责人要求", "项目经理要求", "技术负责人要求"),
}

# 招标标的（采购内容）单独抽，供「技术需求」兜底与标的类型判别使用
CONTENT_LABELS = ("招标内容", "采购内容", "招标范围", "采购范围", "项目概况", "项目规模")


# 同一行里带对象修饰的后续取值（`整机质保 12 个月，易损件质保 18 个月`）：
# 第一个取值仍按原逻辑返回，后续取值以「对象+标签」形式单独成条，避免丢信息。
_OBJECT_QUALIFIED = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{2,8}?)(质保|保修)(?:期)?\s*(?:为|是)?\s*(\d+\s*(?:个月|年|天|日|周))"
)
_OBJ_PREFIX = re.compile(r"^(?:本合同|合同|上述的|上述|该|其|本|此)")


def extract_object_values(noise: str, field: str = "质保期") -> dict[str, str]:
    """收集同一行里带对象修饰的额外取值，返回 {对象+标签: 取值}（例如「易损件质保期」）。"""
    if field != "质保期":
        return {}
    out: dict[str, str] = {}
    for line in (noise or "").splitlines():
        for idx, m in enumerate(_OBJECT_QUALIFIED.finditer(line)):
            if idx == 0:                     # 第一个取值的返回行为保持不变
                continue
            obj = _OBJ_PREFIX.sub("", clean_value(m.group(1)))
            if len(obj) < 2 or any(obj.startswith(x) for x in _FIELD_LABELS):
                continue
            name = obj + ("保修期" if m.group(2) == "保修" else "质保期")
            value = clean_value(m.group(3))
            if name in out or not value_usable(name, value):
                continue
            out[name] = value
    return out


_HEADING_PREFIX = re.compile(r"^[（(【]?[一二三四五六七八九十百\d]{1,3}\s*[）)】、.．]")
_HEADING_ORDINAL = re.compile(r"^[（(【]?[一二三四五六七八九十百\d]{1,2}(?=[\u4e00-\u9fa5])")
_HEADING_TAIL = re.compile(
    r"(?:基本参数|主要参数|参数|技术要求|清单|明细|一览表|说明|概述|概况|范围|内容|标准|规范|要求)$"
)


def _looks_like_heading(value: str) -> bool:
    """章节小标题（`一）支架基本参数`）不是技术需求内容，不能当已确认事实。"""
    v = clean_value(value)
    if len(v) > 24:
        return False
    if _HEADING_PREFIX.match(v):
        return True
    # _balance_brackets 会去掉单边括号（`一）支架基本参数` → `一支架基本参数`），
    # 因此再用「序号前缀 + 标题式结尾」兜一层。
    return bool(_HEADING_ORDINAL.match(v) and _HEADING_TAIL.search(v))


def extract_tender_facts(text: str) -> dict[str, str]:
    """一次性抽出加固后的关键字段（不改变原有 dims 的其它字段）。"""
    noise = strip_tender_noise(text)
    facts: dict[str, str] = {}
    for name, labels in FIELD_LABELS.items():
        value = extract_labeled(noise, labels, field=name)
        if value:
            facts[name] = value
    # 编号走专用抽取器：别名的覆盖面、换行/括号/全角冒号容忍、形态校验与
    # 「封面前 3 页优先 + 出现次数」定夺都在里面（通用取值链做不到这些）。
    # 抽不到时**不编造**：返回值保持为空，只把原文里像编号的串放进诊断字段。
    tender_no = extract_tender_no(text)
    if tender_no.get("value") and tender_no.get("label") not in ("招标项目编号", "项目编号", "项目编码", "招标项目编码", "标段项目编号"):
        facts["招标编号"] = str(tender_no["value"])
    elif not facts.get("招标编号"):
        loose = tender_no.get("candidates") or []
        if loose:
            facts["编号候选"] = "、".join(str(x) for x in loose)
    if tender_no.get("others"):
        facts["其他候选编号"] = "、".join(str(x) for x in tender_no["others"])
    project_name = clean_project_name(_extract_project_name(text, noise))
    if project_name:
        facts["项目名称"] = project_name
    content = extract_labeled(noise, CONTENT_LABELS, max_len=300, field="招标内容")
    if content:
        facts["招标内容"] = content
    contacts = extract_contacts(noise)
    facts.update({k: v for k, v in contacts.items() if v})
    facts["技术规格书要点"] = extract_spec_digest(text)
    # 同一行里带对象修饰的后续取值单独成条（`整机质保 12 个月，易损件质保 18 个月`）
    facts.update(extract_object_values(noise))
    # 「技术需求」应当是采购内容概述，而不是章节小标题（实测抓到过「一）支架基本参数」）
    if content and value_usable("技术需求", content) and not _looks_like_heading(content):
        facts["技术需求"] = content[:200]
    elif not value_usable("技术需求", facts.get("技术需求", "")):
        # 兜底必须是能通过 value_usable 的表述：旧文案含「详见」会被 _NOT_A_VALUE 拒掉，
        # 合并循环于是跳过该维度，垃圾值继续挂着 hit=True 绕过「缺事实即拒绝生成」的准入闸门。
        # 章号不能写死（「第五章」是这份文件的巧合）：按实际找到的供货/技术要求章命名，
        # 找不到就不提章号。
        try:
            from zhibiao.core.tender_checkup import tech_chapter
            _span = tech_chapter(text)
        except Exception:  # noqa: BLE001
            _span = None
        _where = ("%s「%s」" % (_span["label"], _span["title"])) if _span else "技术规格书"
        facts["技术需求"] = "技术参数以招标文件%s要求为准，投标人须逐条响应。" % _where
    return facts


def refresh_tender_facts(dims: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Revalidate old machine-extracted facts for generation/export, without DB writes.

    Explicit user-reviewed/manual facts must never be silently overwritten.
    The caller receives independent rows; persisted interpretation stays intact.
    """
    from copy import deepcopy
    rows = deepcopy(dims)
    manual = {
        str(row.get("name") or ""): deepcopy(row) for row in rows
        if row.get("source_manual") or str(row.get("source") or "").startswith("人工")
        or (isinstance(row.get("review"), dict) and row["review"].get("confirmed"))
    }
    updated = apply_tender_facts(rows, text) if text else rows
    return [manual.get(str(row.get("name") or ""), row) for row in updated]


def apply_tender_facts(dims: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """把加固结果合并进 `analyze()` 产出的 dims（保持原有字段与顺序）。"""
    facts = extract_tender_facts(text)
    by_name = {str(d.get("name") or ""): d for d in dims}
    if facts.get("项目名称") and "招标项目名称" in by_name:
        facts["招标项目名称"] = facts["项目名称"]
    # Old model extractions can look plausible while belonging to another
    # document. Do not carry an unsupported identity into generation/export.
    # refresh_tender_facts restores explicit user-confirmed entries afterward.
    for identity, source_key in (("项目名称", "项目名称"), ("招标项目名称", "项目名称"),
                                 ("项目编号", "项目编号"), ("招标编号", "招标编号")):
        row = by_name.get(identity)
        if row is not None and not facts.get(source_key):
            row["hit"] = False
            row["source"] = ""
    # 没有更好来源时也不能保留代理引用/污染编号为已确认事实。
    for name in ("项目名称", "项目编号"):
        row = by_name.get(name)
        if row is not None and not value_usable(name, row.get("value", "")):
            row["hit"] = False
            row["source"] = ""
    # 1) 覆盖/修正同名维度
    for name, value in facts.items():
        if name == "技术规格书要点":
            continue
        row = by_name.get(name)
        if row is None:
            continue
        if value and value_usable(name, value):
            if name in ("项目名称", "招标项目名称"):
                # 项目名不走 clean_value：它会把名字里合法的 `（第三批）` 当尾垃圾剥掉。
                row["value"] = clean_project_name(value) or clean_value(value)[:300]
            else:
                row["value"] = clean_value(value)[:300]
            row["hit"] = True
            row["source"] = "规则（加固）"
        elif value:
            # 取到的值不可用（残句/掩码/章节小标题）→ 不能算已确认事实，
            # 否则「缺事实即拒绝生成」的准入闸门会被一个章节小标题绕过。
            row["hit"] = False
            row["source"] = ""
    # 2) 明确区分招标人与代理机构联系人，废弃旧的「联系人/联系电话」模糊字段
    for legacy in ("联系人", "联系电话"):
        row = by_name.get(legacy)
        if row is not None:
            row["hit"] = False
            row["value"] = "未识别（需人工确认）"
            row["source"] = ""
    for name in ("招标人联系人", "招标人联系电话", "招标代理联系人", "招标代理联系电话"):
        value = facts.get(name, "")
        row = by_name.get(name)
        if row is None:
            dims.append({"name": name, "value": clean_value(value) or "未识别（需人工确认）",
                         "category": "项目基本信息", "hit": bool(value),
                         "position": -1, "source": "规则（加固）" if value else ""})
        elif value:
            row["value"] = clean_value(value)
            row["hit"] = True
            row["source"] = "规则（加固）"
        else:
            # 旧号码没有当前文档角色块来源时，不允许继续以已确认状态流向封面。
            row["value"] = "未识别（需人工确认）"
            row["hit"] = False
            row["source"] = ""
    # 3) 招标编号单独成条（与项目编号并存）：模型解读常只留一个「编号」维度，
    #    封面于是少打一个号，形式评审会认为投标文件编号不全。
    for name in ("招标编号",):
        value = clean_value(facts.get(name, ""))
        if not value_usable(name, value):
            continue
        row = by_name.get(name)
        if row is None:
            dims.append({"name": name, "value": value, "category": "项目基本信息",
                         "hit": True, "position": -1, "source": "规则（加固）"})
        else:
            row["value"] = value
            row["hit"] = True
            row["source"] = "规则（加固）"
    # 4) 同一行里带对象修饰的后续取值（`整机质保 12 个月，易损件质保 18 个月`）单独成条；
    #    第一个取值的返回行为与既有 dims 结构都不变。
    for name, value in extract_object_values(strip_tender_noise(text)).items():
        if name in by_name:
            continue
        usable = value_usable(name, value)
        dims.append({"name": name, "value": clean_value(value),
                     "category": "项目基本信息", "hit": usable,
                     "position": -1, "source": "规则（加固）" if usable else ""})
    # 5) 项目名/标题统一清洗（所有出口都从 dims 取这个名字）：历史解读结果里可能还存着
    #    被下一行污染的标题，这里按唯一规则收敛一次；已经是干净名字的不变（幂等）。
    for row in dims:
        if str(row.get("name") or "") in ("项目名称", "招标项目名称"):
            cleaned = clean_project_name(row.get("value"))
            if cleaned and cleaned != str(row.get("value") or "").strip():
                row["value"] = cleaned
    return dims
