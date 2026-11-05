"""标的类型判别与大纲模板防错（P0）。

线上实测：煤矿「端头及超前支护技术研究与应用」（端头/超前联排液压支架 + 中置式大阻力端尾支架）
被套用了 IT 软件技术标模板，正文里出现
    「采用 JSON 或 Protobuf 序列化格式」「HTTPS/TLS 1.2」「OAuth 2.0 令牌认证」
    「GB/T 22239 网络安全等级保护」「属地公安机关等保定级备案」「系统渗透测试」
而《煤矿安全规程》、顶板/片帮防控、瓦斯超限断电、乳化液防喷溅、大件下井运输防倾覆
等行业措施全部消失 —— 评标专家一眼就能看出是套错模板。

判别原则（宁可漏改不可错改）：
  * 只在证据充分时给结论；证据不足返回 unknown，**不做任何替换**；
  * 判别只看「标的物描述」（招标内容 / 采购需求 / 项目概况 / 货物清单 等区域）为主，
    全文词频为辅 —— 因为「电液控制系统」「软件永久免费升级」这类词只是产品的附属描述；
  * 替换只做**标题精确匹配**，不做模糊改写。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# ─────────────────────────── 一、判别词典 ───────────────────────────

_STRONG: dict[str, tuple[str, ...]] = {
    "equipment": (
        "液压支架", "端头支架", "超前支架", "端尾支架", "立柱", "千斤顶", "阀组",
        "乳化液", "采煤机", "掘进机", "刮板输送机", "皮带机", "转载机", "破碎机",
        "提升机", "水泵", "减速器", "联排", "初撑力", "工作阻力", "钢板", "焊接",
        "探伤", "台架试验", "出厂检验", "煤安标志", "MA标志", "防爆", "矿用",
        "货物采购", "设备采购", "供货", "备品备件", "易损件", "设备清单",
    ),
    "works": (
        "施工组织", "土建", "建筑工程", "安装工程", "监理", "竣工验收", "混凝土",
        "钢筋", "脚手架", "临时用电", "安全文明施工", "施工方案", "分部分项",
        "工程量清单", "招标控制价", "施工现场", "基坑", "桩基", "装饰装修",
    ),
    "service": (
        "物业服务", "保洁", "绿化养护", "安保服务", "秩序维护", "运维服务",
        "咨询服务", "服务方案", "服务人员", "服务考核", "巡检", "外包服务",
        "劳务派遣", "食堂", "物业管理",
    ),
    "it": (
        "软件开发", "软件系统", "信息系统", "平台开发", "系统开发", "数据中台",
        "数据库建设", "微服务", "云平台", "接口开发", "源代码", "信息化建设",
        "数字化平台", "网络安全等级保护", "等级保护测评", "渗透测试", "等保",
        "APP开发", "小程序开发", "系统集成", "上线部署", "数据治理平台",
    ),
}

_WEAK: dict[str, tuple[str, ...]] = {
    "equipment": ("设备", "机械", "制造", "材质", "规格", "型号", "支架", "机组", "配件"),
    "works": ("施工", "工程", "工期", "质量验收", "现场"),
    "service": ("服务", "人员", "考核", "响应时限"),
    "it": ("软件", "系统", "平台", "信息化", "数据", "接口", "模块", "功能清单"),
}

_TARGET_REGION = re.compile(
    r"(?:招标内容|采购内容|招标范围|采购范围|项目概况|项目规模|货物清单|采购清单|"
    r"技术规格书|技术要求|采购需求|标的|项目内容)"
)

SUBJECT_NAMES = {
    "equipment": "机械装备/货物类",
    "works": "工程施工类",
    "service": "服务类",
    "it": "软件信息化类",
    "unknown": "未判定",
}

# 装备内部的**子类型**：同为煤矿装备，液压支架与带式输送机的专有名词、章节骨架和
# 适用标准完全不同。线上实测：给皮带机项目套用 equipment 通用改写，生成了
# 「总体技术方案与架型选择」「支架压力监测与数据上传」「井下支护安全与顶板管理措施」，
# 并把 GB/T 25974（煤矿用液压支架）写成《带式输送机》。判别只做「证据充分才给结论」。
_SUBTYPE_PATTERNS: dict[str, tuple[str, ...]] = {
    "coal_support": ("液压支架", "端头支架", "超前支架", "端尾支架", "掩护梁", "护帮板",
                     "初撑力", "工作阻力", "立柱", "千斤顶", "乳化液", "联排"),
    "belt_conveyor": ("带式输送机", "皮带机", "输送带", "托辊", "滚筒", "张紧装置",
                      "卷带装置", "逆止器", "盘式制动器", "储带"),
}
SUBTYPE_NAMES = {
    "coal_support": "煤矿液压支架类",
    "belt_conveyor": "带式输送机类",
    "generic": "通用装备类",
}


def equipment_subtype(text: str) -> str:
    """Return coal_support / belt_conveyor / generic using equipment vocabulary only."""
    whole = str(text or "")
    if not whole.strip():
        return "generic"
    scores = {kind: sum(whole.count(word) for word in words)
              for kind, words in _SUBTYPE_PATTERNS.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if top_score >= 4 and top_score >= second * 1.5:
        return top
    return "generic"


def _count(text: str, words: Iterable[str]) -> tuple[int, list[str]]:
    hits: list[str] = []
    score = 0
    for word in words:
        n = text.count(word)
        if n:
            hits.append(word)
            score += n
    return score, hits


def classify_subject(text: str) -> dict[str, Any]:
    """返回 {kind, name, score, evidence, region_hits, confident}。"""
    whole = str(text or "")
    region = ""
    for m in _TARGET_REGION.finditer(whole):
        region += whole[m.start(): m.start() + 1500]
    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for kind in _STRONG:
        s_whole, h_whole = _count(whole, _STRONG[kind])
        s_region, h_region = _count(region, _STRONG[kind])
        w_whole, _ = _count(whole, _WEAK[kind])
        scores[kind] = s_whole * 1.0 + s_region * 3.0 + w_whole * 0.25
        evidence[kind] = (h_region or h_whole)[:8]
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    confident = top_score >= 6.0 and top_score >= second_score * 1.6
    kind = top if confident else "unknown"
    return {
        "kind": kind,
        "name": SUBJECT_NAMES[kind],
        "subtype": equipment_subtype(whole) if kind == "equipment" else "",
        "score": round(top_score, 2),
        "runner_up": ranked[1][0] if len(ranked) > 1 else "",
        "runner_up_score": round(second_score, 2),
        "evidence": evidence.get(top, []) if confident else [],
        "confident": confident,
        "scores": {k: round(v, 2) for k, v in scores.items()},
    }


# ─────────────────────── 二、IT 口味章节的行业化改写 ───────────────────────

REWRITE: dict[str, dict[str, str]] = {
    # 装备子类型未知时的中性口径：不含液压支架专属的「架型 / 支架压力 / 顶板支护」，
    # 否则任何装备项目都会被写成液压支架。
    "equipment": {
        "功能实现与逐条响应": "产品技术方案与参数响应",
        "功能清单与逐项响应": "技术参数逐条响应对照表",
        "核心功能实现方式": "主要结构与工作原理",
        "界面与交互设计": "关键零部件与材质配置",
        "数据治理与接口集成": "设备控制与智能化功能",
        "数据资源规划与标准": "设备配置与标准依据",
        "数据交换与共享机制": "远程操作与自动化功能",
        "第三方系统接口集成": "设备状态监测与数据上传",
        "安全与等保合规设计": "矿用安全与合规设计",
        "等级保护合规设计": "煤矿安全规程符合性",
        "网络安全与边界防护": "防爆与本质安全设计",
        "数据安全与个人信息保护": "井下安全防护与合规措施",
        "应用架构与部署拓扑": "总体结构与主要技术参数",
        "性能与容量设计": "主要性能参数与裕度设计",
        "功能性能与安全测试": "出厂检验与型式试验",
        "系统巡检与版本升级": "设备巡检与维护保养",
        "核心业务功能实现方案": "主要技术方案与实现方式",
        "移动端与自助终端适配": "井下适应性设计与配置",
        "硬件设备配置清单": "主要设备与材料配置清单",
        "软件与许可清单": "随机备件与专用工具清单",
        "国产化适配说明": "关键外购件与品牌配置说明",
        "总体技术架构": "总体技术方案与机型选择",
    },
    "works": {
        "功能实现与逐条响应": "施工方案与技术措施",
        "功能清单与逐项响应": "分部分项工程施工方案",
        "核心功能实现方式": "关键工序技术措施",
        "界面与交互设计": "施工工艺与作业标准",
        "数据治理与接口集成": "施工进度与资源配备",
        "数据资源规划与标准": "总体进度计划与横道图",
        "数据交换与共享机制": "劳动力与机械设备配置",
        "第三方系统接口集成": "材料供应与运输计划",
        "安全与等保合规设计": "安全文明施工与环境保护",
        "等级保护合规设计": "安全生产管理体系",
        "网络安全与边界防护": "危险性较大分部分项工程管控",
        "数据安全与个人信息保护": "文明施工与环境保护措施",
        "应用架构与部署拓扑": "施工总体部署与管理机构",
        "性能与容量设计": "主要施工方案与技术措施",
        "功能性能与安全测试": "工序质量控制与隐蔽验收",
        "系统巡检与版本升级": "回访与维修响应时限",
    },
    "service": {
        "功能实现与逐条响应": "服务方案与作业标准",
        "功能清单与逐项响应": "服务内容与作业标准清单",
        "核心功能实现方式": "服务流程与实施要点",
        "界面与交互设计": "服务岗位与作业规范",
        "数据治理与接口集成": "服务团队与资源配置",
        "数据资源规划与标准": "项目组织架构与岗位职责",
        "数据交换与共享机制": "人员配置与排班计划",
        "第三方系统接口集成": "外部协作与接口管理",
        "安全与等保合规设计": "安全管理与信息保密",
        "等级保护合规设计": "安全管理制度与责任分工",
        "网络安全与边界防护": "现场作业安全管理",
        "数据安全与个人信息保护": "数据与隐私保护措施",
        "应用架构与部署拓扑": "服务总体方案与实施计划",
        "性能与容量设计": "服务响应能力与裕度设计",
        "功能性能与安全测试": "服务质量检查与记录",
        "系统巡检与版本升级": "服务巡检与持续改进",
    },
}

# 液压支架专属骨架：只有确认是煤矿液压支架类项目才使用这套标题。
REWRITE_SUPPORT: dict[str, str] = {
    "数据治理与接口集成": "电液控制系统与智能化功能",
    "数据资源规划与标准": "电液控制系统配置方案",
    "第三方系统接口集成": "支架压力监测与数据上传",
    "数据安全与个人信息保护": "井下支护安全与顶板管理措施",
    "总体技术架构": "总体技术方案与架型选择",
    "应用架构与部署拓扑": "总体结构与主要技术参数",
    "安全与等保合规设计": "矿用安全与合规设计",
    "网络安全与边界防护": "防爆与本质安全设计",
    "功能性能与安全测试": "出厂检验与型式试验",
    "系统巡检与版本升级": "设备巡检与维护保养",
}

# 整套模板就是 IT 口味时，直接换成该行业的主模板
TEMPLATE_FALLBACK = {"equipment": "goods", "works": "construction", "service": "service"}
IT_FLAVOURED_TEMPLATES = ("it",)


# 各子类型**专属**的骨架词：只有确认是该子类型时，这些词才允许出现在章节标题里。
# 其余项目的大纲里出现它们，说明上一版标书/上一份采购文件的骨架被串用了，必须剔除。
# 真实事故：新3（减速器等中小修理）的大纲里长出了「总体技术方案与架型选择」
# 「电液控制系统与智能化功能」「支架压力监测与数据上传」「井下支护安全与顶板管理措施」。
# 注意：只收该子类型**独有**的词。「支架」「电液控制」等词在别的行业不存在，
# 但像「煤矿安全规程符合性」「防爆与本质安全设计」对煤矿机械修理是合理的，绝不收进来。
#
# ── 规则来源约定（用户 2026-10-19 明确要求）──────────────────────────────
# 上面的事故是**示例项目**（上传件只是例子），它只解释这张表为什么存在。
# 表里的词必须是**整个行业/设备类别**通用的专属词，绝不能是某个示例项目的
# 项目名、公司名、型号或它独有的章节名 —— 否则真实标书会被示例特判带偏。
# 标的不确定时不动手（classify_subject 判不准直接给 kind="unknown"，
# pipeline_api 只在 confident 时调用 guard_outline）。
# 回归见 tests/test_no_example_data_in_logic.py。
_SUBTYPE_EXCLUSIVE_TERMS: dict[str, tuple[str, ...]] = {
    "coal_support": ("架型", "初撑力", "工作阻力", "支架压力监测", "护帮板", "掩护梁",
                     "端头支架", "端头架", "超前支架", "端尾支架", "液压支架", "电液控制",
                     "立柱", "千斤顶", "推移千斤顶", "乳化液", "联排", "顶板管理"),
    "belt_conveyor": ("带式输送机", "皮带机", "托辊", "输送带", "张紧装置", "逆止器",
                      "盘式制动器", "储带"),
}


def _foreign_terms_for(subtype: str) -> tuple[str, ...]:
    """返回「本项目不该出现」的专属词：除自己子类型之外，其它子类型的专属词全是外来词。"""
    own = _SUBTYPE_EXCLUSIVE_TERMS.get(str(subtype or ""), ())
    foreign: list[str] = []
    for other, words in _SUBTYPE_EXCLUSIVE_TERMS.items():
        if other == subtype:
            continue
        foreign.extend(w for w in words if w not in own)
    return tuple(foreign)


def foreign_domain_terms(subject: dict[str, Any]) -> tuple[str, ...]:
    """公开入口：本项目不该出现的专属词（交付自检与大纲剔除共用同一张表）。

    标的未判定时返回空元组 —— 判不准就不拦，绝不误伤。
    """
    if not subject or str(subject.get("kind") or "unknown") == "unknown":
        return ()
    return _foreign_terms_for(str(subject.get("subtype") or ""))


def foreign_outline_titles(
    chapters: list[tuple[int, str]],
    subject: dict[str, Any],
) -> tuple[list[tuple[int, str]], list[tuple[str, str]]]:
    """跨域剔除：返回 (保留的章节, [(被剔除的标题, 触发的专属词)])。

    跨项目/跨标段的骨架串用只能在大纲这一层根治 —— 正文阶段再拦，错误的章节名
    已经写进了提示词。这里只在标的已判定时动手（未判定宁可漏改）。
    """
    if not subject or str(subject.get("kind") or "unknown") == "unknown":
        return list(chapters), []
    foreign = _foreign_terms_for(str(subject.get("subtype") or ""))
    if not foreign:
        return list(chapters), []
    kept: list[tuple[int, str]] = []
    dropped: list[tuple[str, str]] = []
    for level, title in chapters:
        text = str(title or "")
        hit = next((word for word in foreign if word in text), "")
        if hit:
            dropped.append((text, hit))
        else:
            kept.append((level, text))
    return kept, dropped


def _rewrite_map_for(subject: dict[str, Any]) -> dict[str, str]:
    """Pick the outline rewrite vocabulary for this subject (equipment subtypes differ)."""
    kind = str(subject.get("kind") or "unknown")
    base = dict(REWRITE.get(kind, {}))
    if kind == "equipment" and str(subject.get("subtype") or "") == "coal_support":
        return {**base, **REWRITE_SUPPORT}
    return base


def guard_outline(
    chapters: list[tuple[int, str]],
    template_keys: list[str],
    subject: dict[str, Any],
    *,
    rewrite_titles: bool = True,
) -> tuple[list[tuple[int, str]], list[str], list[str]]:
    """按标的类型纠正大纲。

    返回 (新章节, 提示, 被替换的标题)。
    * 标的未判定 → 原样返回（宁可漏改）；
    * ``rewrite_titles``（默认真，预设模板路径）→ 对 IT 口味的标题做**精确标题替换**；
      自定义模板/大模型大纲传假，尊重用户与模型自己的口径；
    * 任何已判定标的的情况 → 都做**跨域剔除**（见 foreign_outline_titles）：
      串用了别的标的物骨架的章节必须拿掉，且只能在这一层拿掉。
    """
    kind = str(subject.get("kind") or "unknown")
    if kind == "unknown":
        return chapters, [], []
    notes: list[str] = []
    renamed: list[str] = []
    if not rewrite_titles or kind == "it":
        out: list[tuple[int, str]] = list(chapters)
    else:
        mapping = _rewrite_map_for(subject)
        out = []
        seen: set[str] = set()
        for level, title in chapters:
            new_title = mapping.get(title, title)
            if new_title != title:
                renamed.append(title)
                if new_title in seen:
                    new_title = title          # 重名则保留原名，绝不制造重复章节
            seen.add(new_title)
            out.append((level, new_title))
        if renamed:
            notes.append(
                f"已按标的类型「{SUBJECT_NAMES.get(kind, kind)}」把 {len(renamed)} 个信息化口径的章节名"
                f"改为本行业口径（如「{renamed[0]}」→「{mapping.get(renamed[0], '')}」）"
            )
    out, dropped = foreign_outline_titles(out, subject)
    if dropped:
        notes.append(
            f"已剔除 {len(dropped)} 个与本项目标的物不符的章节："
            + "、".join(f"「{title}」" for title, _word in dropped[:6])
            + (f" 等 {len(dropped)} 章" if len(dropped) > 6 else "")
        )
    if rewrite_titles and any(k in IT_FLAVOURED_TEMPLATES for k in template_keys):
        notes.append(
            f"当前所选模板是「信息化系统集成类」，而本项目标的判定为「{SUBJECT_NAMES.get(kind, kind)}」，"
            f"建议改用「{TEMPLATE_FALLBACK.get(kind, 'general')}」模板（可在「模板与篇幅」里切换）"
        )
    return out, notes, renamed


# ─────────────────────── 三、正文写作的行业口径约束 ───────────────────────

IT_JARGON = (
    "RESTful", "OAuth", "JSON", "Protobuf", "微服务", "中间件", "容器化",
    "Kubernetes", "中台", "API 网关", "令牌认证", "渗透测试", "等保测评",
    "等级保护测评", "数据库加密", "RAID", "缓存击穿", "前后端分离",
)

# 各标的类型**自己的**行业示例。煤矿口径（GB/T 25974、煤矿安全规程、顶板与端头支护、
# 液压支架）只属于煤矿装备：原先不分 kind 一律举煤矿例子，装修/物业项目的提示词里就会
# 出现「端头支护、防爆与本质安全、台架试验」，等于给服务类项目下煤矿命令 ——
# 与本模块开头记载的「套错模板」事故同类，只是方向相反。
# it 类在上面用短句单独返回（行为保持原样），因此不列入本表。
_INDUSTRY_EXAMPLES: dict[str, str] = {
    "works": "（例如 GB 50300 建筑工程施工质量验收统一标准、施工组织设计、分部分项工程施工方案、"
             "隐蔽工程验收、安全文明施工与环境保护、竣工验收资料）。",
    "service": "（例如物业服务与作业规范、岗位配置与排班、服务响应时限与考核办法、"
               "巡检记录与持续改进、客户回访与满意度评价）。",
    "equipment": "（例如出厂检验与型式试验、主要技术参数与裕度设计、安装调试与随机备件保障、"
                 "设备巡检与维护保养）。",
}

# 标的物明确是煤矿装备时才使用煤矿口径（举例：煤矿用液压支架及端头/超前支护设备）。
_EQUIPMENT_COAL_EXAMPLES = (
    "（例如 GB/T 25974、煤矿安全规程、顶板与端头支护、防爆与本质安全、"
    "出厂检验与型式试验、台架试验、安装调试与备件保障）。"
)
_COAL_HINTS = (
    "煤矿", "煤安", "矿用", "液压支架", "端头", "超前", "端尾", "乳化液",
    "采煤", "掘进", "刮板", "转载机", "破碎机", "井下",
)


# 装备子类型专属口径：液压支架的标准与名词不能套到带式输送机上（反之亦然）。
# 只在证据充分时附加；不写具体标准版本号，避免诱导模型编造版本。
_SUBTYPE_GUARD: dict[str, str] = {
    "belt_conveyor": (
        "本项目装备子类型：带式输送机（皮带机）。正文必须使用带式输送机自身的术语与部件"
        "（驱动滚筒、托辊、输送带、张紧装置、卷带装置、逆止器、盘式制动器、储带仓等），"
        "严禁使用液压支架专属的「架型选择、初撑力、工作阻力、支架压力监测、护帮板、掩护梁、"
        "顶板支护管理」等口径；严禁把液压支架类标准号（如 GB/T 25974《煤矿用液压支架》）"
        "写成带式输送机标准。标准号必须与标准名称、适用设备一致；不确定版本年号时只写标准"
        "名称与适用范围，不得虚构版本。"
    ),
    "coal_support": (
        "本项目装备子类型：煤矿液压支架。正文使用支架自身的术语与部件（顶梁、掩护梁、"
        "底座、立柱、千斤顶、护帮板、初撑力、工作阻力、支护强度等），不得套用带式输送机"
        "的滚筒、托辊、输送带、张紧/卷带装置等专属配置。标准号必须与标准名称、适用设备一致。"
    ),
}

# 只在「招标原文完全没有该设备家族」时才算跨品类串用，避免误伤合理对比或通用表述。
# 「电液」是液压支架/电液控制系统的专属口径，真实事故（新3 减速器项目）里章节**正文**仍带着它，
# 而标题级剔除管不到正文，所以正文级也要收。注意「顶板管理」故意不收：煤矿项目谈井下作业
# 顶板/支护安全是合理写法，收进来会误伤「井下安全防护」这类正常章节。
_FOREIGN_TERM_FAMILY: dict[str, tuple[str, ...]] = {
    "架型": ("液压支架", "端头支架", "超前支架", "端尾支架", "架型"),
    "初撑力": ("初撑力", "液压支架"),
    "工作阻力": ("工作阻力", "液压支架"),
    "支架压力": ("支架压力", "液压支架"),
    "护帮板": ("护帮板", "液压支架"),
    "掩护梁": ("掩护梁", "液压支架"),
    "电液": ("电液", "电液控制", "液压支架"),
}

# 明确否认句是正常写法：「本项目修理内容为减速器本体机械部件…**不涉及**电液控制系统集成开发」。
# 这类提法不但不该拦，还是评审愿意看到的范围界定，所以判断跨域词时必须先排除否认语境。
_NEGATION_BEFORE = ("不涉及", "不包括", "不包含", "不含", "无需", "无须", "不采用",
                    "未涉及", "未包含", "不属于", "不作为", "不承担", "非", "无")


def _mentions_are_all_negated(text: str, term: str, window: int = 14) -> bool:
    """该词是否**只**出现在否认语境里（出现多次时，只要有一次是正面陈述就算没被否认）。"""
    start, seen = 0, False
    while True:
        index = text.find(term, start)
        if index < 0:
            return seen
        seen = True
        chunk = text[max(0, index - window):index]
        for stop in "。；;！？!?\n":
            pos = chunk.rfind(stop)
            if pos >= 0:
                chunk = chunk[pos + 1:]
        if not any(word in chunk for word in _NEGATION_BEFORE):
            return False
        start = index + len(term)


def foreign_device_terms(body: str, source: str) -> list[str]:
    """Flag equipment-family terms the tender never uses for this project's equipment.

    Only fires when the source contains none of that family's vocabulary, so a
    legitimate comparison or a genuinely mixed project is never reported.
    """
    text = str(body or "")
    origin = str(source or "")
    hits: list[str] = []
    for term, family in _FOREIGN_TERM_FAMILY.items():
        if term in text and not any(word in origin for word in family) \
                and not _mentions_are_all_negated(text, term):
            hits.append(term)
    return hits


def _is_coal_subject(subject: dict[str, Any]) -> bool:
    """标的物是否明确为煤矿（装备）类 —— 只看已判定的证据词与标的名称。"""
    evidence = subject.get("evidence") or []
    if isinstance(evidence, (list, tuple, set)):
        blob = " ".join(str(item) for item in evidence)
    else:
        blob = str(evidence)
    blob += " " + str(subject.get("name") or "")
    return any(hint in blob for hint in _COAL_HINTS)


def industry_guard_text(subject: dict[str, Any]) -> str:
    """生成注入提示词的行业口径约束（返回结构与调用签名保持不变）。"""
    kind = str(subject.get("kind") or "unknown")
    if kind == "unknown":
        return ""
    name = SUBJECT_NAMES.get(kind, kind)
    if kind == "it":
        return f"本项目标的类型：{name}；正文使用软件信息化的标准术语与措施。"
    jargon = "、".join(IT_JARGON[:8])
    examples = _INDUSTRY_EXAMPLES.get(kind) or "（按本项目标的所属行业的标准、规程与作业规范执行）。"
    if kind == "equipment":
        subtype = str(subject.get("subtype") or "")
        # Subtype is decided first: a belt conveyor in a coal mine is still a conveyor,
        # so it must never inherit the hydraulic-support vocabulary.
        # 子类型专属口径只由下方 guard 追加一次；这里只决定「行业示例」。
        # 旧写法把 _SUBTYPE_GUARD 同时塞进 examples 与 guard，同一段约束在提示词里
        # 出现两遍（既浪费预算又容易让模型把它当成两条独立要求）。
        if subtype == "belt_conveyor":
            examples = _INDUSTRY_EXAMPLES.get("equipment", "")
        elif subtype == "coal_support" or _is_coal_subject(subject):
            # 煤矿液压支架类必须保留煤矿口径（GB/T 25974、《煤矿安全规程》），
            # 否则真实煤矿支架标的会退化成「通用装备」表述，等于丢掉行业依据。
            examples = _EQUIPMENT_COAL_EXAMPLES
        else:
            examples = _INDUSTRY_EXAMPLES.get("equipment", "")
    guard = _SUBTYPE_GUARD.get(str(subject.get("subtype") or ""), "")
    return (
        f"本项目标的类型：{name}。正文必须使用该行业的标准、规程、术语与措施{examples}"
        f"严禁出现与标的无关的信息化措施与术语：{jargon} 等 —— "
        f"出现即视为套错模板，将直接降低评标得分。"
        + (("\n" + guard) if guard else "")
    )


CONTACT_RULE = (
    "联系人口径：招标文件里的联系人只用于澄清、答疑、递交等程序性表述；"
    "严禁把招标代理机构（或平台客服）的联系人、电话写成招标人/采购人/使用单位的"
    "现场负责人、生产调度或应急抢修电话。"
    # 招标人自己的信息不该由投标人去"待填"：原文有的直接引用，
    # 原文没有的写「由招标人指定」。实测旧口径让一份技术标出现 24 处
    # 「{{待填:招标人现场负责人及电话}}」——既是形式评审风险，也让正文像在要求甲方自报信息。
    "招标人联系人、电话、现场负责人一律不得留待填：原文已给出的（如“联系方式”一节）直接引用；"
    "原文未给出的写「由招标人指定」或省略。只有**投标人自己**的人员、证书、业绩、"
    "报价等真实材料缺失时，才写 {{待填:投标人现场负责人及电话}}。"
)