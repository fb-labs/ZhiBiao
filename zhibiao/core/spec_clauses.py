"""原文条款抽取：材质与工艺、涂装与防腐、特殊装置（如破顶装置）、运输与包装。

线上事故（第一份6，唐安端头支架）：
* 2.2.6 / 3.2.3 写破顶装置，缺「适用巷道高度 3.0-3.5m、收回 2.7m、伸出 4.0m」，
  也缺破顶立柱 缸径/柱径 320/290mm、破顶力 2532kN、左右摆动 100/70/310、
  前后摆动 100/70/400 —— 这些数字全在招标文件第 58 页那张表里，但从未进上下文。
* 材质与涂装写成「按招标人要求确定」「按原文规定选材」，而原文第 55～57 页写得很明确：
  「直径大于Φ40 销轴均采用 30CrMnTi 棒材；其余均采用 40Cr 棒材。销轴均镀锌、钝化处理」
  「立柱热处理镗磨、活塞体电镀镀铬、均喷银色油漆、阻燃抗静电」
  「一层底漆，两层面漆，面漆为白色和红色。立柱和千斤顶为银色」。

根因：`param_lock` 只收「××支架基本参数表」里的整架/立柱/千斤顶行，条文型条款
（材质、工艺、涂装、特殊装置参数）既不进台账也不进参数表，模型只能写空话。

本模块把这类条款整段抽出来注入上下文，并提供核对函数：
* `render_clause_context` —— 只注入与本章相关的条款 + 一份关键的「关键条款摘要」；
* `key_clauses` —— 单句条款（涂装/镀层/材质牌号/工艺），供硬核对与提示；
* `section_for` —— 取某个关键词所在章节的原文（破顶装置、运输、包装等）。
"""

from __future__ import annotations

import re
from typing import Any

# 条款类关键词：命中即视为「必须照抄的条文」，不得改写成「按招标人要求确定」
MATERIAL_WORDS = ("材料", "材质", "工艺", "防腐", "涂装", "表面处理", "焊接", "密封件")
DEVICE_WORDS = ("破顶", "特殊装置", "专用装置", "调角", "摆动")
TRANSPORT_WORDS = ("运输", "包装", "贮存", "外形尺寸", "外形")

_ALL_WORDS = MATERIAL_WORDS + DEVICE_WORDS + TRANSPORT_WORDS

_HEADING = re.compile(
    r"^\s*(?:[（(][一二三四五六七八九十百]+[)）]"
    r"|[一二三四五六七八九十百]+[、.]"
    r"|\d+(?:\.\d+){0,3}[.、]?"
    r"|[（(]\d+[)）])"
)
# 单句条款：含涂装/镀层/牌号/工艺等实质要求（必须从句子边界开始，避免截出半句）
_CLAUSE = re.compile(
    r"(?:^|(?<=[。；：\n]))[^。\n]{6,160}?(?:底漆|面漆|喷漆|油漆|涂装|镀锌|钝化|镀铬|镗磨"
    r"|银色|白色和红色|阻燃|抗静电|棒材|聚氨酯|销轴|打磨清洁|激光下料|气体保护焊)[^。\n]{0,120}。?"
)
_NOISE_WORDS = ("山西兰花", "招标代理", "招标文件", "=====", "投标人须", "本招标")
_NOISE_RE = re.compile(r"第\s*\d+\s*页")


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


_NOISE = re.compile(
    r"^(?:=+\s*第\s*\d+\s*页\s*=+$"
    r"|招标文件$"
    r"|\[表格\]$"
    r"|.*(?:电话[:：]\s*0?\d{2,4}[－-]\d{6,8}).*$"
    r"|.*邮箱\s*\d+@.*$)",
)
_PAGE_FURNITURE = ("招标文件", "[表格]")
_MAX_LINE = 220


def clean_text(text: str) -> str:
    """去掉 PDF 页眉页脚（页标记、代理公司电话/邮箱、[表格] 标记），否则会把两段无关内容粘在一起。"""
    out: list[str] = []
    for raw in str(text or "").replace("\r\n", "\n").splitlines():
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if _NOISE.match(line):
            continue
        if line in _PAGE_FURNITURE:
            continue
        out.append(line)
    return "\n".join(out)


def unwrap(text: str) -> str:
    """PDF 抽出来的句子常被硬回车切断（「其余均采用」/「40Cr 棒材。」），先接回去，
    否则条款会被拆成两半。表格行不参与拼接；单行拼接上限 _MAX_LINE，避免把不同小节粘死。"""
    out: list[str] = []
    for line in clean_text(text).splitlines():
        if not line:
            out.append("")
            continue
        if out and out[-1] and not out[-1].startswith("|") and not line.startswith("|") \
                and not re.search(r"[。；：！？|]\s*$", out[-1]) \
                and not _looks_like_heading(out[-1]) and not _looks_like_heading(line) \
                and len(out[-1]) + len(line) <= _MAX_LINE:
            out[-1] = out[-1] + line
        else:
            out.append(line)
    return "\n".join(out)


def _looks_like_heading(line: str) -> bool:
    """标题行：短、带编号或含条款关键词，且**不是表行**（表行里全是数值/单位）。
    实测「1 破顶立柱 缸径/柱径 mm 320/290 供煤安证」会被误判成标题，
    结果破顶力 2532、行程 310/400 被切到别的节里，核对时当作"原文没有"。"""
    if re.search(r"\|", line) or re.search(r"\b(?:mm|kN|MPa|t|kg)\b", line):
        return False
    # 括号没闭合的短行是表格单元格被 PDF 折断的残片（「调角千斤顶（左右」），不是标题
    if line.count("（") != line.count("）") or line.count("(") != line.count(")"):
        return False
    if len(re.findall(r"\d", line)) >= 4 and len(line) > 16:
        return False
    if len(line) <= 28 and _HEADING.match(line) and not re.search(r"[。；，,]", line):
        return True
    if len(line) <= 24 and any(word in line for word in _ALL_WORDS) and not re.search(r"[。；，]", line):
        return True
    return False


def extract_sections(tender_text: str) -> list[dict[str, Any]]:
    """按「标题行」把原文切成小节。标题行 = 短行且带编号/顿号，或含条款关键词。"""
    lines = unwrap(tender_text).splitlines()
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            if current is not None:
                current["lines"].append("")
            continue
        if _looks_like_heading(line):
            current = {"heading": line, "lines": []}
            sections.append(current)
        elif current is not None:
            current["lines"].append(line)
    for section in sections:
        section["body"] = "\n".join(section.pop("lines")).strip()
    return sections


def _relevant(section: dict[str, Any], words: tuple[str, ...]) -> bool:
    """章节相关性只看标题：正文里顺带出现「材料」二字（如投标报价条款）不算材质章节。"""
    return any(word in section["heading"] for word in words)


def section_for(tender_text: str, word: str, max_chars: int = 1600) -> str:
    """取包含某关键词的章节原文（用于破顶装置、运输包装这类整段条款）。"""
    chunks: list[str] = []
    for section in extract_sections(tender_text):
        if word in section["heading"] or word in section.get("body", "")[:800]:
            block = (section["heading"] + "\n" + section.get("body", "")).strip()
            if block and block not in chunks:
                chunks.append(block)
        if sum(len(chunk) for chunk in chunks) >= max_chars:
            break
    return "\n\n".join(chunks)[:max_chars]


def key_clauses(tender_text: str, limit: int = 12) -> list[str]:
    """单句条款：涂装/镀层/材质牌号/表面处理等，去掉重复与空话。"""
    found: list[str] = []
    seen: set[str] = set()
    for match in _CLAUSE.finditer(unwrap(tender_text)):
        clause = _compact(match.group())
        if not (8 <= len(clause) <= 170):
            continue
        if any(word in clause for word in _NOISE_WORDS) or _NOISE_RE.search(clause):
            continue
        # 被页脚切断后剩下的半句（「部设置加强筋板…」「的稳定机构…」）没有引用价值
        if clause[0] in "的了和与及或部理书且并第":
            continue
        key = re.sub(r"\s+", "", clause)[:24]
        if key in seen:
            continue
        seen.add(key)
        found.append(clause)
        if len(found) >= limit:
            break
    return found


def explicit_values(tender_text: str) -> bool:
    """原文是否给了明确的涂装/材质要求（用来禁止「按招标人要求确定」这类空话）。"""
    return bool(re.search(r"底漆|面漆|喷漆|油漆|镀锌|钝化|镀铬|镗磨|棒材|阻燃|抗静电", str(tender_text or "")))


def render_clause_context(tender_text: str, path: str = "", focus: str = "",
                          detail_budget: int = 1800) -> str:
    """注入原文条款。相关章节给整段，其余章只给关键条款摘要（控制 token）。"""
    if not str(tender_text or "").strip():
        return ""
    hint = f"{path} {focus}"
    detail: list[str] = []
    used = 0
    for section in extract_sections(tender_text):
        if not _relevant(section, _ALL_WORDS):
            continue
        # 整段注入只给「本章主题就命中这个关键词」的章节，避免 48 章都塞一遍同样的长段
        matched = [word for word in _ALL_WORDS if word in section["heading"] and word in hint]
        if not matched:
            continue
        block = (section["heading"] + "\n" + section.get("body", "")).strip()
        if not block or block in detail:
            continue
        if used + len(block) > detail_budget:
            block = block[: max(0, detail_budget - used)]
        detail.append(block)
        used += len(block)
        if used >= detail_budget:
            break

    clauses = key_clauses(tender_text)
    parts: list[str] = ["【原文条款（材质/工艺/涂装/特殊装置）——照抄，不得改写成"
                        "「按招标人要求确定」「按原文规定选材」】"]
    if detail:
        parts.append("以下为本项目招标原文相关条款，正文引用时必须与原文一致：")
        parts.extend(detail)
    if clauses:
        parts.append("关键条款（逐条可引用，数值/牌号不得改动）：")
        parts.extend("· " + clause for clause in clauses)
    if len(parts) == 1:
        return ""
    parts.append("注意：原文已明确规定材质牌号、镀层、涂装颜色与层数的，必须照抄具体值；"
                 "只有原文确实未规定时，才可写「以招标人确认的设计文件为准」。")
    return "\n".join(parts)


def numbers_near(text: str, keyword: str, window: int = 120) -> list[str]:
    """取关键词附近出现的带单位数值（用于「破顶装置」这类特殊装置的数值核对）。"""
    values: list[str] = []
    for match in re.finditer(re.escape(keyword), str(text or "")):
        segment = text[max(0, match.start() - window): match.end() + window]
        values.extend(re.findall(r"\d+(?:\.\d+)?(?:\s*[-~～—至]\s*\d+(?:\.\d+)?)?", segment))
    return values