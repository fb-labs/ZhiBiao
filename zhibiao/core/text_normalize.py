"""生成后的确定性文字归一：单位笔误、章节交叉引用。

这两类错误靠模型自己几乎不会修（它不知道目录、也不知道「湿度用 ℃」是错的），
而它们又小到不值得为它们判整章失败。所以放在生成后、质检前做机械改写：

* 湿度写成摄氏度：「相对湿度≤75℃」→「相对湿度≤75%」（温度才用 ℃）；
* 指向不存在章节的引用：「按 10.1.1.3 第（3）项」→ 实际章节「10.1.3」
  （只在该编号不存在、且缩短后**唯一**命中真实章节时才改，不做模糊猜测）。
"""

from __future__ import annotations

import re

# 湿度 + 数值 + 摄氏度：只改「湿度」这一句里的单位，温度不受影响。
_HUMIDITY_C = re.compile(r"((?:相对)?湿度[^。；！？\n]{0,14}?[≤≤<＜>=＞]?\s*\d+(?:\.\d+)?)\s*(?:℃|°C|°Ｃ|摄氏度)")

# 交叉引用：见/按/参见/依据 等 + 形如 10.1.1.3 的编号
_REF = re.compile(r"(?P<lead>见|按|参见|参照|依据|详见|详见第|依照|遵循)\s*"
                  r"(?P<num>\d{1,2}(?:\.\d{1,2}){1,4})(?P<tail>\s*(?:节|章|条|项|部分)?)")


def fix_humidity_unit(text: str) -> tuple[str, int]:
    """「相对湿度≤75℃」→「相对湿度≤75%」。返回 (新文本, 改动处数)。"""
    count = 0

    def _repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return match.group(1) + "%"

    return _HUMIDITY_C.sub(_repl, str(text or "")), count


def fix_chapter_references(text: str, numbers: set[str] | frozenset[str]) -> tuple[str, int]:
    """把指向不存在章节的引用改成真实章节号（仅在唯一候选时）。

    线上案例：正文「按 10.1.1.3 第（3）项闭环处理」，实际章节是 10.1.3 —— 多打了一段。
    处理方式：编号不存在时，逐个删掉其中一段，若恰有一个结果命中真实章节号，就整体替换；
    命中零个或多个一律不动（宁可留着也不乱改引用）。
    """
    source = str(text or "")
    known = {str(item).strip() for item in (numbers or set()) if str(item).strip()}
    if not source or not known:
        return source, 0
    fixes: dict[str, str] = {}
    for match in _REF.finditer(source):
        num = match.group("num")
        if num in known or num in fixes:
            continue
        parts = num.split(".")
        candidates: list[str] = []
        duplicate_hits: list[str] = []
        for index in range(len(parts)):
            shortened = ".".join(parts[:index] + parts[index + 1:])
            if not shortened or shortened not in known:
                continue
            candidates.append(shortened)
            # 「多打了一段」的典型形态是相邻两段重复：10.1.1.3 → 10.1.3
            prev_same = index > 0 and parts[index] == parts[index - 1]
            next_same = index + 1 < len(parts) and parts[index] == parts[index + 1]
            if prev_same or next_same:
                duplicate_hits.append(shortened)
        candidates = list(dict.fromkeys(candidates))
        duplicate_hits = list(dict.fromkeys(duplicate_hits))
        if len(duplicate_hits) == 1:
            fixes[num] = duplicate_hits[0]
        elif len(candidates) == 1:
            fixes[num] = candidates[0]
    if not fixes:
        return source, 0
    out = source
    for wrong, right in fixes.items():
        out = re.sub(r"(?<![\d.])" + re.escape(wrong) + r"(?![\d.])", right, out)
    return out, len(fixes)


# 招标文件里的空白填空：`设备质保期【一】年`、`【　】`、`【××】`、`【填写】`、`【盖章】`。
# 第四章合同条款模板满篇都是这种写法，模型会连同括号一起抄进正文（实测 3 章），
# 导出后就是「质保期【一】年」——评标专家一眼看出是把未填的合同模板当成了承诺。
_BRACKET_FILL = re.compile(
    r"【\s*(?:[一二三四五六七八九十百千两\d]{1,4}|[×xX＊*]{2,4}|_{2,}|[ 　]{0,3}|"
    r"填写|填写处|盖章|盖章处|签字|签字处|年|月|日)\s*】")
# 括号前的名词是填空对象：`质保期【一】年` → 待填「质保期」
_OBJECT_TAIL = re.compile(r"([\u4e00-\u9fa5]{2,8})$")


def fix_template_brackets(text: str) -> tuple[str, int]:
    """把招标模板里的【一】【　】填空改成红色待填占位 {{待填:对象}}。

    只改「方括号里是数字序号/叉号/空格/填写盖章字样」的纯填空；
    【图片占位】、【注】这类有内容的方括号不动。
    """
    source = str(text or "")
    count = 0

    def _repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        before = source[:match.start()]
        noun = _OBJECT_TAIL.search(before.replace("\n", " ").rstrip("，,、；;：:"))
        label = (noun.group(1) if noun else "").strip()
        if not label:
            return "{{待填:招标文件未给出的具体值}}"
        return "{{待填:" + label + "}}"

    out = _BRACKET_FILL.sub(_repl, source)
    return out, count


def normalize_generated_text(text: str, numbers: set[str] | frozenset[str] | None = None) -> tuple[str, list[str]]:
    """生成后统一归一，返回 (新文本, 改动说明列表)。

    改动说明进 chapter.source 的可追溯后缀，方便核对「这段是机器改的还是模型写的」。
    """
    notes: list[str] = []
    out, humidity = fix_humidity_unit(text)
    if humidity:
        notes.append("湿度单位归一")
    out, refs = fix_chapter_references(out, numbers or set())
    if refs:
        notes.append(f"交叉引用归一({refs})")
    out, brackets = fix_template_brackets(out)
    if brackets:
        notes.append(f"模板填空括号转待填({brackets})")
    return out, notes