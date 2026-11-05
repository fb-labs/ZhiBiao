"""「信用/信誉查询证明」小节：渠道清单从招标原文来，逐渠道一行 + 每行一个图框。

背景（线上问题）
----------------
第 14.3 节《信用与合规查询证明》生成出来是这样：

* 文字部分只列了 2 个渠道（国家企业信用信息公示系统、信用中国），
  招标文件「信誉要求」里点名的第三个渠道「中国执行信息公开网」整节没出现；
* 下面也只放了 2 个图框，但图框里的说明文字写着「三个渠道各一张」——

也就是说：**正文条数、图框数量、图框说明三者互相矛盾**，靠提示词让模型「记得写全」
是治不住的（模型每次漏的渠道还不一样）。这里改成代码兜底：

    渠道清单 = 招标原文里实际点名的查询渠道（不编造）
    正文行数 = 图框数量 = 渠道数（构建时天然成立，`credit_proof_gaps()` 可复核）

口径
----
* **逐渠道一行**：每个渠道写一行「渠道名 + 查什么」，紧跟一个 `{{图片占位:…}}`
  （语法与说明文案全部来自 `app/core/image_slots.py`，本模块不自造文案）；
* **缺失兜底**：招标原文点名不全时，缺的渠道**不补造**，只在文末追加一行
  「其他」兜底，提示出件人按招标文件补齐；
* 招标原文一个渠道都没点名时不接管这一节（返回 None / 原样返回），避免无依据改写。

消费方
------
* 生成侧：`generation_runner` 材料分支（`credit_proof_body()`），与
  `legal_forms` / `format_forms` 同级——这类小节的内容由招标人规定，不该交给模型；
* 整册后处理：`ensure_credit_channels()`，把存量/单章重跑的正文按同一口径归一。
"""

from __future__ import annotations

import re

from zhibiao.core.image_slots import IMAGE_SLOT_LINE, image_placeholder

#: 招标文件「信誉要求 / 信用查询」里可能点名的查询渠道（标准三个）。
#: 顺序 = 投标文件里通常的排列顺序，也是 `image_slots` 里截图口径的排列顺序。
#: 关键字只认**站点/平台名**，不拿「失信被执行人」这类业务词去猜渠道，
#: 否则招标原文写一句「无失信被执行人记录」就会被误判成一个渠道。
#: 注意「全国法院失信被执行人名单信息公布与查询平台」与「中国执行信息公开网」是同一个
#: 渠道（同一网站），归到一条，否则招标原文两种写法都写到时会出现第 4 行。
CREDIT_CHANNEL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("国家企业信用信息公示系统",
     ("国家企业信用信息公示系统", "全国企业信用信息公示系统")),
    ("信用中国", ("信用中国",)),
    ("中国执行信息公开网",
     ("中国执行信息公开网", "执行信息公开网", "全国法院失信被执行人名单")),
)

#: 招标原文点名不全时的兜底行（不编造缺失的渠道，留给人工按原文补齐）。
OTHER_CHANNEL = "其他"

#: 每个渠道「查什么」：写进正文那一行，出件人一眼知道该截哪张图。
CHANNEL_FOCUS: dict[str, str] = {
    "国家企业信用信息公示系统": "查询投标人主体登记信息、经营异常名录、"
                              "严重违法失信企业名单（黑名单）与行政处罚记录",
    "信用中国": "查询失信被执行人、重大税收违法失信主体与政府采购严重违法失信行为记录",
    "中国执行信息公开网": "查询投标人失信被执行人记录",
    OTHER_CHANNEL: "招标文件点名的其他信用查询渠道",
}

#: 「其他」那一行的图框说明。命中 image_slots 的「查询截图」口径，
#: 因此图框里显示的是「信誉查询网页截图（其他渠道的信用查询截图；截全网址…）」。
_OTHER_IMAGE_KIND = "其他渠道的信用查询截图"

# ── 标题口径：只有「查询证明 / 信誉材料」类小节才接管 ─────────────────────────
# 主题词（讲的是哪一类证明）
_TITLE_TOPICS = ("信用", "信誉", "资信", "失信")
# 任务词（要求提交/查询/截图，而不是写方案）
_TITLE_TASKS = ("证明", "查询", "截图", "材料", "证照", "网页", "要求", "记录")
# **渠道名**：这类小节的标题在招标文件里经常是纯罗列，一个任务词都不带 ——
# 实例：《(二) 信用中国失信被执行人、全国企业信用信息公示系统严重违法失信企业 查询记录》。
# 只要标题点到渠道名，讲的就是"这一节要交哪些渠道的查询记录"，必须接管；
# 反过来靠「证明/截图」这类任务词去判，会把「XXX 查询记录」这种法定小节整节漏掉（废标项）。
_TITLE_CHANNELS = ("信用中国", "企业信用信息公示系统", "执行信息公开网", "信用记录", "失信")
# 反例：这些标题写的是做法、制度或文书本身，不能整节换成渠道清单
_TITLE_PROSE = ("管理", "制度", "方案", "分析", "流程", "措施", "体系", "培训", "计划", "建设",
                "承诺", "授权书", "声明", "函")

def credit_proof_scope(title: str) -> bool:
    """这一节是不是「信用/信誉查询证明」类小节（内容 = 渠道清单 + 查询截图）。

    两道门槛任一命中即接管：

    1. **标题点到任一查询渠道名**（信用中国 / 企业信用信息公示系统 / 执行信息公开网 /
       信用记录 / 失信）—— 招标文件常用「渠道一、渠道二 查询记录」这种罗列式标题；
    2. 老口径：主题词 + 任务词同时命中（信用 + 证明/查询/截图…）。

    写的是做法/制度/文书本身（管理、制度、方案、承诺、声明…）的一律不接管。
    """
    clean = re.sub(r"\s+", "", str(title or ""))
    if not clean:
        return False
    if any(word in clean for word in _TITLE_PROSE):
        return False
    if any(word in clean for word in _TITLE_CHANNELS):
        return True
    if not any(word in clean for word in _TITLE_TOPICS):
        return False
    if not any(word in clean for word in _TITLE_TASKS):
        return False
    return True


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def credit_channels_in_source(source_text: str) -> list[str]:
    """招标原文实际点名的查询渠道（按固定顺序去重；一个都没点名时返回空列表）。"""
    flat = _flat(source_text)
    if not flat:
        return []
    found: list[str] = []
    for name, keywords in CREDIT_CHANNEL_RULES:
        if any(keyword in flat for keyword in keywords) and name not in found:
            found.append(name)
    return found


def credit_channel_rows(source_text: str) -> list[str]:
    """本节应有的渠道行（= 正文行数 = 图框数量）。

    招标原文点名了几个渠道就写几行；**点名不全时不补造**，只在末尾追加一行「其他」，
    由出件人按招标文件补齐（旧实现是让模型自由发挥，于是漏渠道、图框数与说明互相矛盾）。
    只点名 1 个渠道的不算「渠道清单」（那更像正文里顺带提了一句），此时不接管这一节，
    避免把资格审查类小节误改写成渠道清单；判据与质检口径（≥2 个渠道才算清单）一致。
    """
    named = credit_channels_in_source(source_text)
    if len(named) < 2:
        return []
    rows = list(named)
    if len(named) < len(CREDIT_CHANNEL_RULES):
        rows.append(OTHER_CHANNEL)
    return rows


def row_text(index: int, channel: str, source_text: str = "") -> str:
    """渠道行的文字：一行一个渠道，说明查什么（不再复述别的渠道）。"""
    focus = CHANNEL_FOCUS.get(channel, "按招标文件要求查询")
    if channel == OTHER_CHANNEL:
        return "%d. %s：%s（上述名单未覆盖的，如有时一并提供查询截图）。" % (
            index, channel, focus)
    # 招标原文把「中国执行信息公开网」写全称时，正文也必须写全称：质检按原文点名的
    # 站点名逐个核对，少一个字都会被判「本章漏了该查询渠道」。
    alias = ""
    if channel == "中国执行信息公开网" and "全国法院失信被执行人名单" in _flat(source_text):
        alias = "（全国法院失信被执行人名单信息公布与查询平台）"
    return "%d. %s：%s%s，查询结果见下方查询网页截图。" % (index, channel, focus, alias)


def row_slot(channel: str) -> str:
    """该行下面的图框：语法与说明文案全部交给 image_slots，本模块不另写。"""
    kind = _OTHER_IMAGE_KIND if channel == OTHER_CHANNEL else channel
    return image_placeholder(kind)


def build_credit_proof_body(source_text: str) -> str:
    """按招标原文的渠道清单生成这一节：N 行文字 + N 个图框（严格一一对应）。"""
    rows = credit_channel_rows(source_text)
    if not rows:
        return ""
    blocks: list[str] = []
    for index, channel in enumerate(rows, start=1):
        blocks.append(row_text(index, channel, source_text))
        blocks.append(row_slot(channel))
    return "\n".join(blocks)


def credit_proof_body(title: str, source_text: str) -> str | None:
    """生成侧入口：不属于这类小节、或招标原文没给出渠道清单时返回 None（不接管）。"""
    if not credit_proof_scope(title):
        return None
    body = build_credit_proof_body(source_text)
    return body or None


# ── 复核 / 归一 ──────────────────────────────────────────────────────────────

def slot_lines_of(body: str) -> list[str]:
    """正文里的图框行（独立成行的 `{{图片占位:…}}`）。"""
    return [line.strip() for line in str(body or "").splitlines()
            if IMAGE_SLOT_LINE.fullmatch(line.strip())]


def credit_proof_gaps(body: str, source_text: str) -> list[str]:
    """这一节的确定性缺口：行数、图框数、图框说明与渠道清单必须一一对应。

    返回空列表 = 口径一致；返回非空 = `ensure_credit_channels()` 会整节重建。
    """
    rows = credit_channel_rows(source_text)
    if not rows:
        return []
    issues: list[str] = []
    text = str(body or "")
    for channel in rows:
        hits = [line for line in text.splitlines()
                if channel in line and not IMAGE_SLOT_LINE.fullmatch(line.strip())]
        if len(hits) != 1:
            issues.append("渠道「%s」在正文中出现 %d 行（应为 1 行）" % (channel, len(hits)))
    slots = slot_lines_of(text)
    if len(slots) != len(rows):
        issues.append("图框 %d 个与渠道 %d 个不一致" % (len(slots), len(rows)))
    elif len(rows) == len(slots):
        # 图框说明必须说的是自己那个渠道，否则就是「2 个图框却写三个渠道各一张」那类硬伤
        for channel, slot in zip(rows, slots):
            caption = _OTHER_IMAGE_KIND if channel == OTHER_CHANNEL else channel
            if caption not in slot:
                issues.append("「%s」的图框说明没有写明本渠道" % channel)
    return issues


def ensure_credit_channels(body: str, source_text: str,
                           title: str = "") -> tuple[str, str]:
    """把正文按「逐渠道一行 + 一图框」归一；口径已一致时原样返回。

    返回 (新正文, 处理说明)。说明为空表示没改动。给了标题且不是这类小节时不动。
    """
    if title and not credit_proof_scope(title):
        return body, ""
    rows = credit_channel_rows(source_text)
    if not rows:
        return body, ""
    gaps = credit_proof_gaps(body, source_text)
    if not gaps:
        return body, ""
    rebuilt = build_credit_proof_body(source_text)
    if not rebuilt:
        return body, ""
    return rebuilt, "信誉查询渠道按招标原文逐条重建（%d 个渠道 = %d 行文字 + %d 个图框）" % (
        len(rows), len(rows), len(rows))