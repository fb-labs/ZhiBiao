"""跨章重复压缩（B 档）：同一口径在多个章节里近乎逐字复述时，只保留专章那一份，
其余章节改成**一行中性引用**。目标是减少成稿里的同义复述，不是删内容。

线上实测（项目 bee64b91-0c87-4609-bea3-948c8b946b58，32 章、106,217 字）：

* 售后四件套（``2 小时回复 / 24 小时到场 / 10 个工作日修复 / 15 日内免费更换``）
  跨 6~9 章近乎逐字，只差「我方/乙方」「甲方/招标人」；
* 质保期起算一句跨 9 章、质保金返还一句跨 4 章、培训安装责任一句跨 3 章；
* 清单套（随机附件/随机备件/技术资料/说明书/图册）各跨 10~11 章；
* 表格层面 0 重复（79 个表块，最高 Jaccard 0.12）—— 所以表格只做回归护栏，不改。

判冗余的三条口径（阈值全部是模块常量，便于调参）：

1. **近似逐字句**：规范化（去空白、全半角统一、我方/乙方归一等）后相似度
   ≥ :data:`NEAR_DUP_SIMILARITY`，且跨 ≥ :data:`NEAR_DUP_MIN_CHAPTERS` 章出现；
2. **商务口径**（带承诺数值的句子：响应/质保/交付/培训/备件比例…）跨
   ≥ :data:`COMMERCIAL_MIN_CHAPTERS` 章、数值签名完全相同、相似度
   ≥ :data:`COMMERCIAL_SIMILARITY` 时判冗余（口径可换词，数值不许变）；
3. **表格**：行指纹 Jaccard ≥ :data:`TABLE_ROW_JACCARD` 且行数 ≥ :data:`TABLE_MIN_ROWS`
   —— 本数据 0 命中，只作为回归护栏（只出记录、不改正文）。

五条红线（实现上都是硬保护，宁可不压）：

* **格式文书章一字不动**：照录招标文件格式的章节（投标函/身份证明/授权/联合体/
  保证金/偏差表/报价表/资格审查/承诺书）永不改写；同一句同时出现在格式章与正文时，
  保留格式章那份，引用也指向格式章；
* **技术参数不参与去重**：带单位数值的句子（MPa/kN/mm/°/L/min/% 及 φ、V、A、kW、
  kg、℃、㎡ 等同族单位）一律不动 —— 那是各章的举证点，与「重复」无关；
* **仅标题级重复不算冗余**：只统计正文句子，标题行/表格行/占位行从不参与；
* **已是引用式不动**：「详见第 X 章」「见 8.2 节」「…本节不重复…」既不改写也不再采集，
  这正是压缩后的目标形态；
* **不删专章首处**：保留「该口径的专职章」整段原文；**定不出专职章（并列最高分或
  无命中）就整组放弃**，宁可漏压，不可错压；
* **整章不压空**：压完不得少于 :data:`MIN_CHAPTER_CHARS` 字、也不得少掉原章一半 ——
  整章几乎全是重复句，更可能是同一段被复制到好几章的生成异常，压成一行引用会像漏写，
  这种情况整章不动，留人工核对。

压缩动作只有一种：把这一段里的重复句摘掉，在**第一处**落一行中性引用，例如
``售后响应时限见第 12.4 节，本节不重复。``——不改数值、不编内容、
不加审判词（不出现「冗余」「重复表述」「错误」之类的评价），也不动文档结构与章序。
一段里若同时有两条口径的重复句（售后时限 + 质保金返还），按专职章**各落一处引用**：
只落一处等于把另一条口径删掉却不告诉读者去哪看，那是丢信息，不是压缩。

性能：逐章线性。句子按「规范化哈希 + 二元组稀有倒排」分桶，只在代表句的小桶里做
相似度比较，没有全句两两比较（106k 字实测约 0.3 秒，其中约 0.5 秒是一次性的
``app.core.config`` 导入开销，不计入；生成的倒排候选对不到 900 个）。
幂等：第二次运行命中 0 处（引用行不再参与采集，成组只剩专章一份）。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Callable, Iterable

# ══════════════════════ 一、阈值常量（调参只改这里） ══════════════════════

#: 近似逐字判据：规范化后的相似度下限
NEAR_DUP_SIMILARITY = 0.85
#: 近似逐字判据：至少跨几章出现才算冗余（1 章内的自我重复不在本项目范围内）
NEAR_DUP_MIN_CHAPTERS = 3
#: 商务口径判据：换词同口径时放宽到的相似度下限
COMMERCIAL_SIMILARITY = 0.72
#: 商务口径判据：至少跨几章出现才算冗余
COMMERCIAL_MIN_CHAPTERS = 5
#: 句长下限：更短的句子多是「一 年」「年 月 日」这类格式残片，不参与
MIN_SENTENCE_CHARS = 8
#: 压缩后至少要净省这么多字才动手（否则引用行比原文还长，越压越多）
MIN_NET_SAVING = 6
#: 整章兜底：压完不得少于这么多字，也不得少掉原章一半以上 —— 「整章几乎全是重复句」
#: 更可能是同一段被复制到好几章的生成异常，压成一行引用会像漏写，留给人工核对
MIN_CHAPTER_CHARS = 240
MIN_CHAPTER_KEEP_RATIO = 0.5
#: 表格行指纹判据（护栏，不改正文）
TABLE_ROW_JACCARD = 0.6
TABLE_MIN_ROWS = 3
#: 候选对上限：病态输入（上千句几乎同文）时兜底，避免退化成全量比较
MAX_PAIR_CHECKS = 60_000

# ══════════════════════ 二、规范化 ══════════════════════

# 主体称谓归一：只归「同一主体的不同写法」，不碰金额、型号、参数。
_SELF_WORDS = ("我方", "我公司", "本公司", "乙方")
_BUYER_WORDS = ("甲方", "招标人", "采购人", "需方")
_ALIAS: dict[str, str] = {word: "\u00abS\u00bb" for word in _SELF_WORDS}
_ALIAS.update({word: "\u00abB\u00bb" for word in _BUYER_WORDS})
_ALIAS_RE = re.compile("|".join(sorted(map(re.escape, _ALIAS), key=len, reverse=True)))

# 技术参数标记：数值 + 单位。命中即整句不参与去重（各章举证点，绝不删）。
_TECH_UNITS = (
    r"MPa|kPa|hPa|Pa|kN|N·m|N\.m|Nm|mm|cm|dm|km|mL|ml|L/min|l/min|L·min|"
    r"m2|m²|㎡|m3|m³|kW|kV|V|mA|A|Hz|kg|t|℃|°|%|φ|Φ|Ø|目|dB"
)
_TECH_RE = re.compile(r"\d\s*(?:" + _TECH_UNITS + r")(?![A-Za-z])")

# 句子切分：中文句末/分句标点都算边界（「；」切出来的是一口气里的一个分项）
_SENT_END = "。；！？!?;"
# 段首编号与短标签：「（2）响应时限：」「3. 」「二、」
_LABEL_RE = re.compile(
    r"^[ \t\u3000]*((?:[（(][ \t]*[\d一二三四五六七八九十]+[ \t]*[)）]"
    r"|\d+[.．、)）]|[一二三四五六七八九十]+[、.．]))[ \t]*"
    r"([^。；！？\n：:，,]{1,14}[：:])?"
)
# 占位符：待填/图片框是**逐章**要填的槽位，压缩掉会让待填清单少一项，一律不动
_PLACEHOLDER_RE = re.compile(r"\{\{")
# 已是引用式（压缩后的目标形态）：整行不动、也不再采集
_REFERENCE_RE = re.compile(
    r"本节不重复|详见\s*第?\s*[\d一二三四五六七八九十]+|见\s*第\s*[\d.]+\s*[章节]"
    r"|参见\s*第?\s*[\d.]+\s*[章节]|由\s*[\d.]+\s*至\s*[\d.]+\s*节"
)
# 承上启下的指代：删掉句子会让「上述/前述」落空，整段放弃压缩
_ANAPHORA = ("上述", "前述", "前款", "该款", "该等", "该项", "前项", "以上所述", "如前所述")

# 商务口径：句子既要有承诺动作，又要有承诺数值（数值本身绝不改）
_COMMIT_WORDS = (
    "响应", "回复", "到场", "到达", "修复", "维修", "更换", "质保", "保修", "免费",
    "交付", "交货", "供货", "到货", "工期", "培训", "安装", "调试", "备件", "配件",
    "随机", "比例", "承诺", "小时内", "工作日内", "日内", "天内",
)
_DEADLINE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:个?小时|个?工作日|个?日历日|日|天|个月|年|%|％|套|件|倍)"
)

# 主题词表：用来判断「这一组句子属于哪个口径」。命中任一主题即可
# 用它来判断哪一章是「该口径的专职章」。
_TOPIC_WORDS: dict[str, tuple[str, ...]] = {
    "售后响应": ("售后", "响应", "时限", "维修", "修复", "更换", "回复", "到场", "派员"),
    "质保": ("质保", "保修", "质保金", "保质"),
    "备件": ("备件", "备品", "易损件", "随机附件", "随机", "配件"),
    "培训": ("培训", "授课", "操作指导"),
    "安装调试": ("安装", "调试", "试运行"),
    "交付": ("交付", "交货", "供货", "运输", "到货", "发运", "交付期"),
    "技术资料": ("资料", "图册", "说明书", "图纸", "文档", "清单"),
    "制造工艺": ("工艺", "制造", "熔覆", "镀", "镗", "焊", "热处理", "涂装"),
    "检测试验": ("检测", "试验", "检验"),
    "业绩信誉": ("业绩", "信誉", "信用", "用户证明"),
    "报价商务": ("报价", "价格", "单价", "总价", "零整比"),
    "履约能力": ("履约", "产能", "财务", "资质"),
}
# 引用行的前缀短语（中性描述，不含任何评价）
_TOPIC_LABEL: dict[str, str] = {
    "售后响应": "售后响应时限",
    "质保": "质保期口径",
    "备件": "备件与随机附件",
    "培训": "培训安排",
    "安装调试": "安装调试验收",
    "交付": "交付安排",
    "技术资料": "技术资料清单",
    "制造工艺": "制造工艺要求",
    "检测试验": "检测与试验安排",
    "业绩信誉": "业绩与信誉材料",
    "报价商务": "报价口径",
    "履约能力": "履约能力说明",
}


# 法定格式文书的章名词（标题兜底判据）：命中即整章永不改写。
# 线上实测这 9 类分别是第 1–8、11、30 章（投标函/身份证明/授权/联合体/保证金/
# 偏差表/报价表/资格审查/承诺书），换成别的标书也还是这几种文书，只是章号不同。
_FORM_TITLE_WORDS = (
    "投标函", "响应函", "报价函", "申请函", "投标文件格式",
    "身份证明", "授权委托书", "法定代表人授权", "授权书",
    "联合体协议", "投标保证金", "保证金",
    "偏差表", "报价一览表", "分项报价表", "开标一览表", "价格表",
    "资格审查", "资格证明", "承诺书", "声明函",
)


# ══════════════════════ 三、句子切分与判据 ══════════════════════


def _normalize(sentence: str) -> str:
    """规范化：全半角统一、去空白、主体称谓归一。"""
    value = unicodedata.normalize("NFKC", str(sentence or ""))
    value = re.sub(r"\s+", "", value)
    return _ALIAS_RE.sub(lambda m: _ALIAS[m.group(0)], value)


def _strip_label(sentence: str) -> str:
    """去掉段首编号与短标签（「（2）响应时限：」），只留实质内容。"""
    match = _LABEL_RE.match(sentence or "")
    return sentence[match.end():] if match else sentence


def _canon(sentence: str) -> str:
    """比对用的规范形：先去编号标签，再规范化。"""
    return _normalize(_strip_label(str(sentence or "")))


def _split_sentences(line: str) -> list[tuple[int, int, str]]:
    """把一段切成 [(起, 止, 句子)]，偏移量相对该行。"""
    out: list[tuple[int, int, str]] = []
    start = 0
    for index, char in enumerate(line):
        if char in _SENT_END:
            out.append((start, index + 1, line[start:index + 1]))
            start = index + 1
    if line[start:].strip():
        out.append((start, len(line), line[start:]))
    return [item for item in out if item[2].strip()]


def _bigrams(text: str) -> set[str]:
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _is_prose_line(line: str) -> bool:
    """只处理正文段落：表格行、标题行、占位行、引用行都不是压缩对象。"""
    value = line.strip()
    if not value:
        return False
    if value.startswith(("|", "#", ">", "```", "~~~", "{{", "- ", "* ", "+ ")):
        return False
    if _PLACEHOLDER_RE.search(value):
        return False
    return _REFERENCE_RE.search(value) is None


def _is_tech_sentence(sentence: str) -> bool:
    """带单位数值的句子属于技术参数举证，绝不参与去重。"""
    return _TECH_RE.search(sentence or "") is not None


def _is_commercial(sentence: str) -> bool:
    value = str(sentence or "")
    return bool(_DEADLINE_RE.search(value)) and any(word in value for word in _COMMIT_WORDS)


def _number_signature(sentence: str) -> tuple[str, ...]:
    """数值签名：口径可以换词，数值不许变，签名相同才允许按商务口径合并。"""
    value = unicodedata.normalize("NFKC", str(sentence or ""))
    return tuple(re.findall(r"\d+(?:\.\d+)?\s*(?:个?小时|个?工作日|个?日历日|日|天|个月|年|%|％)"
                           r"|\d+(?:\.\d+)?", value))


def _topics_of(text: str) -> list[str]:
    return [name for name, words in _TOPIC_WORDS.items() if any(word in text for word in words)]


# ══════════════════════ 四、分桶与成组 ══════════════════════


def _pair_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _group(canons: list[str], *,
           min_similarity: float,
           accept: Callable[[int, int], bool] | None = None) -> dict[int, list[int]]:
    """按「先出现的那句」当代表句成组：成员必须与代表句相似度达标，组与组之间不做传递。

    为什么不用并查集做传递闭包：A≈B、B≈C 会把 A 和 C 并成一族，
    实测把「质保期起算」「培训安装责任」「备件清单」三件不同的事粘成一组，
    于是专职章被判成了不相干的章（错压）。这里改成「只跟代表句比」：
    组内一致性天然有界，代表句就是**文档顺序的首处**。

    候选只从「精确哈希桶 + 三个最稀有二元组桶」里取（倒排只登记代表句），
    没有全句两两比较；106k 字实测候选对不到 900 个。
    """
    exact: dict[str, list[int]] = {}
    grams: dict[str, list[int]] = {}
    freq: dict[str, int] = {}
    group_of: dict[int, int] = {}
    checked = 0
    for index, canon in enumerate(canons):
        if not canon:
            group_of[index] = index
            continue
        bag = _bigrams(canon)
        candidates: set[int] = set(exact.get(canon, ()))
        if bag:
            for gram in sorted(bag, key=lambda item: freq.get(item, 0))[:3]:
                candidates.update(grams.get(gram, ()))
        best: tuple[float, int] | None = None
        for rep in candidates:
            if checked >= MAX_PAIR_CHECKS:
                break
            checked += 1
            if accept is not None and not accept(index, rep):
                continue
            left, right = canon, canons[rep]
            if abs(len(left) - len(right)) > 0.35 * max(len(left), len(right)) + 4:
                continue
            score = _pair_similarity(left, right)
            if score >= min_similarity and (best is None or score > best[0]):
                best = (score, rep)
        if best is not None:
            group_of[index] = best[1]
            continue
        # 成为新的代表句
        group_of[index] = index
        exact.setdefault(canon, []).append(index)
        for gram in bag:
            grams.setdefault(gram, []).append(index)
            freq[gram] = freq.get(gram, 0) + 1
    groups: dict[int, list[int]] = {}
    for index, root in group_of.items():
        groups.setdefault(root, []).append(index)
    return groups


# ══════════════════════ 五、专职章判定 ══════════════════════


def _keep_chapter(group_seqs: list[Any], chapters: dict[Any, dict[str, Any]],
                  containers: set[str], numbers: dict[str, str],
                  group_text: str) -> tuple[Any, str] | None:
    """定这一组的「专职章」：定不出（并列最高分 / 无命中）就返回 None（整组不压）。

    **只看章标题**，内嵌小标题和正文都不算分。两条理由都是实测逼出来的：

    * 正文算分 ⇒ 「谁写得多谁当专章」，真正该保留口径的章反被压掉；
    * 小标题算分 ⇒ 噪声压过信号：「质保期起算」那句里带「调试运行」，扩出来的
      安装调试词表就命中了《培训计划》的小标题「培训与安装调试的衔接」，
      于是专职章被判成《培训计划》，质保口径被压成一句「见第 12.3 节」。

    容器章（自身还有下级章的总述章）不参与竞争：它本来就是引用下级章节的角色。
    """
    topics = _topics_of(group_text)
    if not topics:
        return None
    words: set[str] = set()
    for name in topics:
        words.update(_TOPIC_WORDS[name])
    scores: dict[Any, int] = {}
    for seq in group_seqs:
        chapter = chapters.get(seq)
        if chapter is None:
            continue
        number = str(numbers.get(str(chapter.get("id") or ""), "") or "")
        if number and number in containers:
            continue          # 容器章是总述，它本来就在引用下级章节
        if str(chapter.get("_format")) == "1":
            continue
        title = str(chapter.get("title") or "")
        score = sum(1 for word in words if word in title)
        if score:
            scores[seq] = score
    if not scores:
        return None
    best = max(scores.values())
    winners = [seq for seq, score in scores.items() if score == best]
    if len(winners) != 1:
        return None           # 并列：说不清谁是专职章，宁可漏压
    keeper_seq = winners[0]
    keeper_title = str(chapters[keeper_seq].get("title") or "")
    # 引用前缀用「与专职章标题对得上的那个主题」，否则会出现
    # 「备件与随机附件见第 12.3 节（培训计划）」这种自相矛盾的引用
    label_topic = topics[0]
    best_hits = -1
    for name in topics:
        hits = sum(1 for word in _TOPIC_WORDS[name] if word in keeper_title)
        if hits > best_hits:
            best_hits, label_topic = hits, name
    return keeper_seq, label_topic


# ══════════════════════ 六、主流程 ══════════════════════


def _chapter_number(chapter: dict[str, Any], numbers: dict[str, str]) -> str:
    key = str(chapter.get("id") or "")
    if key and key in numbers:
        return str(numbers[key])
    return str(numbers.get(str(chapter.get("seq") or ""), "") or "")


def _reference_line(keeper_title: str, keeper_number: str, topic: str,
                    with_label: bool) -> str:
    """中性引用行：只写「哪一节有」+「本节不重复」，不带任何评价词。

    有章号就只写章号（``售后响应时限见第 12.4 节，本节不重复。``）—— 越短，
    「引用行比原文还长」的情形就越少；拿不到章号才退回写章名。
    """
    head = _TOPIC_LABEL.get(topic, "") if with_label else ""
    if keeper_number:
        return "%s见第 %s 节，本节不重复。" % (head, keeper_number)
    place = ("「%s」" % keeper_title) if keeper_title else "对应章节"
    return "%s见%s，本节不重复。" % (head, place)


def _rewrite_paragraph(line: str, removable: list[bool],
                       paragraph_plans: list[dict[str, Any] | None],
                       chapters: dict[Any, dict[str, Any]],
                       numbers: dict[str, str],
                       ) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """把这一段里的重复句摘掉，在**第一处**落一行中性引用。

    一段里可能同时有两条不同口径的重复句（售后时限 + 质保金返还）。这时按专职章
    分组，**各落一处引用** —— 只落一处会把另一条口径的内容删掉却不告诉读者去哪看，
    那就是丢信息，不是压缩。

    返回 (新行, 记录, 该段第一处命中的方案)；没有可压的句子或压完不省字时返回 None。
    只动这一段：段号、标题、表格行、换行位置都不变，其他段落原样透传。
    """
    pieces = _split_sentences(line)
    if len(pieces) != len(removable) or not any(removable):
        return None
    begin = removable.index(True)
    # 按专职章归并（保持首次出现顺序），一条口径一处引用
    ordered: list[dict[str, Any]] = []
    seen: dict[Any, dict[str, Any]] = {}
    for index, flag in enumerate(removable):
        if not flag:
            continue
        plan = paragraph_plans[index]
        if plan is None:
            continue
        key = plan.get("keeper")
        if key not in seen:
            chapter = chapters.get(key)
            if chapter is None:
                continue
            title = str(chapter.get("title") or "").strip()
            if not title:
                continue
            seen[key] = {"plan": plan, "title": title,
                         "number": _chapter_number(chapter, numbers)}
            ordered.append(seen[key])
    if not ordered:
        return None
    removed = "".join(pieces[index][2] for index, flag in enumerate(removable) if flag).strip()
    if not removed:
        return None
    before = line[:pieces[begin][0]]
    survivors = [pieces[index][2] for index, flag in enumerate(removable)
                 if not flag and index > begin]
    after = "".join(survivors) + line[pieces[-1][1]:]
    label = ""
    if begin == 0:
        match = _LABEL_RE.match(pieces[0][2])
        if match and match.group(1):
            label = match.group(0).strip()
    # 前面刚是「……：」时不重复写主题短语，避免「响应时限：售后响应时限见…」
    with_label = not label and not before.rstrip().endswith(("：", ":"))
    references = []
    for position, entry in enumerate(ordered):
        references.append(_reference_line(
            entry["title"], entry["number"], entry["plan"].get("topic") or "",
            with_label or position > 0))
    reference = "".join(references)
    if len(removed) - len(reference) < MIN_NET_SAVING:
        return None                     # 压完反而更长，不动
    head = before.rstrip()
    if head.endswith(("，", ",", "；", ";", "、")):
        head = head[:-1] + "。"
    tail = after
    while tail[:1] in ("，", ",", "；", ";", "、", "。"):
        tail = tail[1:]
    new_line = head + label + reference + tail
    new_line = re.sub(r"[ \t]{2,}", " ", new_line)
    # 摘句后可能留下「。。」「；。」这类连续标点，统一收成一个句号
    new_line = re.sub(r"(?<=[。；！？])[；，、。]+", "", new_line)
    new_line = re.sub(r"。[ \t]*$", "。", new_line)
    if new_line == line:
        return None
    first = ordered[0]
    recorded = removed[len(label):].strip() if label and removed.startswith(label) else removed
    return new_line, {
        "removed": recorded,
        "replacement": reference,
        "keeper_number": first["number"],
        "keeper_title": first["title"],
        "topic": first["plan"].get("topic") or "",
        "keepers": [entry["number"] or entry["title"] for entry in ordered],
    }, first["plan"]


def compress_cross_chapter_duplicates(
    chapters: Iterable[dict[str, Any]] | None,
    tender_text: str = "",
    *,
    numbers: dict[str, str] | None = None,
    enabled: bool | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """跨章重复压缩（B 档）。返回 ({章节 id: 新正文}, 处理记录)；无改动返回 ({}, [])。

    * ``chapters``：整册章节（需要 ``id``/``seq``/``level``/``title``/``content``，
      可选 ``source`` 用来认格式文书章）；
    * ``tender_text``：招标原文，用于「照录格式章」的二次判定（拿不到就不判）；
    * ``numbers``：章节号映射（id → 12.4 这种）；不传就按 ``chapter_rules.section_numbers``
      自己算一遍，与导出侧同一口径；
    * ``enabled``：``None`` 时读配置 ``CROSS_CHAPTER_DEDUP``（默认开启）。

    纯函数：只读入参，不改入参 dict；不抛异常（任何意外都返回空改动）。
    """
    if enabled is None:
        enabled = _enabled_by_config()
    if not enabled:
        return {}, []
    rows = [dict(row) for row in (chapters or []) if isinstance(row, dict)]
    if len(rows) < NEAR_DUP_MIN_CHAPTERS:
        return {}, []
    try:
        return _run(rows, str(tender_text or ""), numbers)
    except Exception:  # noqa: BLE001 - 成稿治理不允许因压缩失败而中断
        return {}, []


def _enabled_by_config() -> bool:
    try:
        from zhibiao.core.config import get_settings

        return bool(getattr(get_settings(), "CROSS_CHAPTER_DEDUP", True))
    except Exception:  # noqa: BLE001 - 取不到配置就按默认开启
        return True


def _format_sequences(rows: list[dict[str, Any]], tender_text: str) -> set[Any]:
    """照录招标格式的章节（永不改写）。

    两道判据取并集，宁可多认一章格式章（少压），也不许把法定文书压掉：
    1. 与导出侧同一个口径 —— ``output_guard.is_format_form_chapter``（source 标记，
       或拿得到招标原文时按「格式章里确有原文小节」二次判定）；
    2. **标题兜底**：线上存在 source 被人工编辑清空、任务又是旧版留下的情况，
       这时按法定文书名认（投标函/身份证明/授权委托书/联合体协议/保证金/偏差表/
       报价表/资格审查/承诺书…），不看招标原文也能认出来。
    """
    from zhibiao.core.output_guard import is_format_form_chapter

    found: set[Any] = set()
    for row in rows:
        title = str(row.get("title") or "").strip()
        if any(word in title for word in _FORM_TITLE_WORDS):
            found.add(row.get("seq"))
            continue
        if is_format_form_chapter(str(row.get("source") or ""), title, tender_text):
            found.add(row.get("seq"))
    return found


def _section_numbers(rows: list[dict[str, Any]], numbers: dict[str, str] | None) -> dict[str, str]:
    if numbers:
        return {str(key): str(value) for key, value in numbers.items()}
    from zhibiao.core.chapter_rules import section_numbers

    mapped = section_numbers(rows)
    for row in rows:                      # 没 id 的调用方（测试/离线分析）按 seq 兜底
        key = str(row.get("id") or "")
        if key and key in mapped:
            continue
        mapped.setdefault(str(row.get("seq") or ""), str(row.get("seq") or ""))
    return mapped


def _build_plans(rows: list[dict[str, Any]], tender_text: str,
                 numbers: dict[str, str] | None) -> dict[str, Any]:
    """成组 + 定专职章：返回本次压缩的全部上下文（改写的唯一依据）。"""
    from zhibiao.core.chapter_rules import container_chapter_numbers

    mapped = _section_numbers(rows, numbers)
    containers = container_chapter_numbers(mapped.values())
    format_seqs = _format_sequences(rows, tender_text)
    chapters = {row.get("seq"): row for row in rows}
    for row in rows:
        row["_format"] = "1" if row.get("seq") in format_seqs else "0"

    # ── 采集候选句：正文段落、整句、非参数、非占位、非引用 ──────────────
    seqs: list[Any] = []
    texts: list[str] = []
    canons: list[str] = []
    commercial: list[bool] = []
    signatures: list[tuple[str, ...]] = []
    pool: dict[tuple[Any, str], int] = {}
    for row in rows:
        seq = row.get("seq")
        for line in str(row.get("content") or "").splitlines():
            if not _is_prose_line(line):
                continue
            for _, _, piece in _split_sentences(line):
                sentence = piece.strip()
                if len(sentence) < MIN_SENTENCE_CHARS:
                    continue
                if _is_tech_sentence(sentence):
                    continue
                canon = _canon(sentence)
                if len(canon) < MIN_SENTENCE_CHARS:
                    continue
                key = (seq, canon)
                if key in pool:
                    continue              # 同一章内自我重复不在本项目范围内
                pool[key] = len(canons)
                seqs.append(seq)
                texts.append(sentence)
                canons.append(canon)
                commercial.append(_is_commercial(sentence))
                signatures.append(_number_signature(sentence))
    state: dict[str, Any] = {
        "numbers": mapped,
        "containers": containers,
        "format_seqs": format_seqs,
        "chapters": chapters,
        "pool": pool,
        "plans": {},
    }
    if not canons:
        return state

    # ── 成组：① 近似逐字（跨 ≥3 章） ② 商务口径（跨 ≥5 章、数值签名相同） ──
    def commercial_accept(left: int, right: int) -> bool:
        return commercial[left] and commercial[right] and signatures[left] == signatures[right]

    plans: dict[int, dict[str, Any]] = state["plans"]
    for groups, min_chapters, reason in (
        (_group(canons, min_similarity=NEAR_DUP_SIMILARITY),
         NEAR_DUP_MIN_CHAPTERS, "near_duplicate"),
        (_group(canons, min_similarity=COMMERCIAL_SIMILARITY, accept=commercial_accept),
         COMMERCIAL_MIN_CHAPTERS, "commercial_term"),
    ):
        for root, members in groups.items():
            group_seqs = sorted({seqs[index] for index in members}, key=_order_key)
            if len(group_seqs) < min_chapters:
                continue
            if any(index in plans for index in members):
                continue              # 已经被更强的判据认领
            group_text = "".join(texts[index] for index in members)
            keeper = _decide_keeper(group_seqs, chapters, format_seqs, containers,
                                    mapped, group_text)
            if keeper is None:
                continue              # 说不清谁是专职章：整组放弃（宁可漏压）
            keeper_seq, topic = keeper
            for index in members:
                if seqs[index] == keeper_seq or seqs[index] in format_seqs:
                    continue          # 专章那份与格式文书章一律留着
                plans[index] = {
                    "keeper": keeper_seq,
                    "topic": topic,
                    "reason": reason,
                    "chapters": group_seqs,
                }

    return state


def _run(rows: list[dict[str, Any]], tender_text: str,
         numbers: dict[str, str] | None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    state = _build_plans(rows, tender_text, numbers)
    plans = state["plans"]
    if not plans:
        return {}, []
    pool = state["pool"]
    chapters = state["chapters"]
    mapped = state["numbers"]
    format_seqs = state["format_seqs"]

    # ── 逐章改写：一段里最长的一段连续重复句换成一行引用 ────────────
    updates: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for row in rows:
        seq = row.get("seq")
        if seq in format_seqs:
            continue                      # 红线：格式文书章一字不动
        content = str(row.get("content") or "")
        if not content.strip():
            continue
        status = str(row.get("status") or "").strip()
        if status and status != "已生成":
            continue
        ending = "\r\n" if "\r\n" in content else "\n"
        changed = False
        chapter_records: list[dict[str, Any]] = []
        out_lines: list[str] = []
        for line in content.splitlines():
            if not _is_prose_line(line):
                out_lines.append(line)
                continue
            pieces = _split_sentences(line)
            removable: list[bool] = []
            paragraph_plans: list[dict[str, Any] | None] = []
            for _, _, piece in pieces:
                index = pool.get((seq, _canon(piece.strip())))
                plan = plans.get(index) if index is not None else None
                paragraph_plans.append(plan)
                removable.append(bool(plan))
            if not any(removable):
                out_lines.append(line)
                continue
            if any(mark in line for mark in _ANAPHORA):
                out_lines.append(line)    # 「上述/前述」落空风险：整段放弃
                continue
            result = _rewrite_paragraph(line, removable, paragraph_plans, chapters, mapped)
            if result is None:
                out_lines.append(line)
                continue
            new_line, record, first_plan = result
            out_lines.append(new_line)
            changed = True
            chapter_records.append({
                "chapter_id": str(row.get("id") or ""),
                "chapter_seq": seq,
                "chapter_title": str(row.get("title") or "").strip(),
                "chapter_number": _chapter_number(row, mapped),
                "reason": first_plan["reason"],
                "duplicate_chapters": first_plan["chapters"],
                **record,
            })
        if changed:
            new_content = ending.join(out_lines)
            # 整章几乎全是重复句 ⇒ 更可能是「同一段被复制到好几章」的生成异常；
            # 这时压成一行引用，成稿看起来就是「这一章漏写了」。整章不动，留人工核对。
            floor = max(MIN_CHAPTER_CHARS, int(len(content) * MIN_CHAPTER_KEEP_RATIO))
            if len(new_content) < floor:
                continue
            updates[str(row.get("id") or "")] = new_content
            records.extend(chapter_records)
    return updates, records


def _order_key(value: Any) -> tuple[int, Any]:
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value))


def _decide_keeper(group_seqs: list[Any], chapters: dict[Any, dict[str, Any]],
                   format_seqs: set[Any], containers: set[str],
                   numbers: dict[str, str], group_text: str) -> tuple[Any, str] | None:
    """定专职章：出现在格式章里就保留格式章那份（红线），否则按主题找唯一的专职章。"""
    formatted = [seq for seq in group_seqs if seq in format_seqs]
    if formatted:
        topic = (_topics_of(group_text) or [""])[0]
        return formatted[0], topic
    return _keep_chapter(group_seqs, chapters, containers, numbers, group_text)


def table_duplicate_report(chapters: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """表格行指纹护栏：行 Jaccard ≥ 阈值且行数 ≥ 阈值才报，**不改正文**。

    线上真实数据 79 个表块、最高 Jaccard 0.12 —— 这里预期 0 命中，
    留作回归护栏：以后有人把整表复制到另一章时能被报出来。
    """
    rows = [dict(row) for row in (chapters or []) if isinstance(row, dict)]
    blocks: list[tuple[Any, tuple[tuple[str, ...], ...]]] = []
    for row in rows:
        current: list[tuple[str, ...]] = []
        for line in str(row.get("content") or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("|"):
                cells = tuple(cell.strip() for cell in stripped.strip("|").split("|"))
                if any(cells) and not all(set(cell) <= set("-: ") for cell in cells if cell):
                    current.append(cells)
                continue
            if current:
                blocks.append((row.get("seq"), tuple(current)))
                current = []
        if current:
            blocks.append((row.get("seq"), tuple(current)))
    report: list[dict[str, Any]] = []
    for index, (left_seq, left_rows) in enumerate(blocks):
        if len(left_rows) < TABLE_MIN_ROWS:
            continue
        for right_seq, right_rows in blocks[index + 1:]:
            if right_seq == left_seq or len(right_rows) < TABLE_MIN_ROWS:
                continue
            left_set = {tuple(sorted(cells)) for cells in left_rows}
            right_set = {tuple(sorted(cells)) for cells in right_rows}
            shared = len(left_set & right_set)
            union = len(left_set | right_set)
            if not union:
                continue
            jaccard = shared / union
            if jaccard >= TABLE_ROW_JACCARD:
                report.append({
                    "left_seq": left_seq, "right_seq": right_seq,
                    "rows": min(len(left_rows), len(right_rows)),
                    "jaccard": round(jaccard, 3),
                })
    return report