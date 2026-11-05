from __future__ import annotations

import re
from dataclasses import dataclass, field

CN_NUM = "一二三四五六七八九十"
FLOW_TERMS = re.compile(r"评标|授予合同|定标|中标通知|签订合同|质疑|投诉|废标|重新招标|投标人须知")
FORMAT_HINT = re.compile(
    r"投标文件(格式|编制|组成|目录|内容)|响应文件(格式|编制|组成|内容)|"
    r"报价文件格式|格式及要求|文件格式"
)
CHAPTER_LINE = re.compile(r"^[ \t]*第([%s\d]{1,3})章[ \t、.．:：]*([^。，,；;]{2,36})" % CN_NUM)
CN_SECTION = re.compile(r"^[ \t]*([%s]{1,3})[、.．][ \t]*([^0-9。，,；;：:]{2,26})[ \t]*$" % CN_NUM)
PAREN_SECTION = re.compile(r"^[ \t]*[（(]([%s]{1,3})[）)][ \t]*([^。，,；;：:]{2,26})" % CN_NUM)

# 招标文件只给出「技术方案」「服务保障方案」这种总章时，按评分项/常规交付项展开下级，
# 顶层章名与顺序仍然完全照抄招标文件，不改名、不增删。
_TEMPLATE_CHILDREN: dict[str, tuple[str, ...]] = {
    "技术方案": (
        "总体架构与技术路线",
        "功能需求逐条响应",
        "数据治理与安全设计",
        "系统集成与接口方案",
        "实施方案与进度计划",
    ),
    "服务保障方案": ("项目团队与职责", "培训方案", "运维与售后服务", "质保期与备件承诺"),
    "报价文件": ("分项报价表", "报价说明"),
    "资格证明文件": (
        "营业执照与主体资格",
        "软件企业或高新技术企业证书",
        "同类项目业绩",
        "财务报告与纳税社保",
    ),
}
_TECH_SCORE = re.compile(r"总体架构|功能需求|数据治理|系统集成|实施方案|技术方案|测试|验收|安全")
_SERVICE_SCORE = re.compile(r"团队|培训|运维|售后|质保|服务保障")


@dataclass
class SectionPlan:
    heading: str
    intent: str = ""
    level: int = 1
    weight: float = 1.0
    target_words: int = 0
    scores: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "heading": self.heading,
            "intent": self.intent,
            "level": self.level,
            "targetWords": self.target_words,
            "scores": self.scores,
        }


STANDARD_TEMPLATE: tuple[tuple[str, float, tuple[tuple[str, str], ...]], ...] = (
    (
        "商务响应",
        0.8,
        (
            ("投标函", "按招标文件格式作出投标承诺，明确投标有效期与报价总价口径。"),
            ("法定代表人身份证明与授权委托", "提供法定代表人身份证明；如由代理人签署，附授权委托书。"),
            ("商务条款响应与偏差说明", "逐条响应付款方式、交付地点、质保与违约责任，仅作无负偏离声明。"),
            ("投标保证金", "说明保证金金额、缴纳方式与退还安排。"),
        ),
    ),
    (
        "资格证明文件",
        0.7,
        (
            ("营业执照与主体资格", "证明独立法人资格与经营范围覆盖本项目。"),
            ("行业资质与认证", "列明与本项目相关的资质、体系认证及有效期。"),
            ("类似项目业绩", "提供同类项目合同关键页与验收证明的清单。"),
            ("财务报告与纳税社保", "说明财务状况、纳税与社保缴纳情况。"),
            ("无重大违法记录声明", "按招标文件要求出具声明并加盖公章。"),
        ),
    ),
    (
        "技术方案",
        1.7,
        (
            ("项目理解与建设目标", "说明项目背景、建设范围、难点与总体目标。"),
            ("总体架构与技术路线", "给出总体架构、技术选型与关键设计原则。"),
            ("功能需求逐条响应", "按采购内容逐条响应，明确功能边界与实现方式。"),
            ("数据治理与安全设计", "说明数据采集、治理、权限、审计与等保要求。"),
            ("系统集成与接口方案", "说明与既有系统的集成方式、接口协议与联调计划。"),
            ("实施方案与进度计划", "给出里程碑、资源投入、并行安排与风险控制。"),
            ("测试验收方案", "覆盖单元、集成、性能与验收标准，明确交付物。"),
        ),
    ),
    (
        "服务保障",
        1.0,
        (
            ("项目团队与职责", "说明团队结构、角色职责与关键人员投入。"),
            ("培训方案", "覆盖管理员、业务用户与运维人员的培训安排。"),
            ("运维与售后服务", "明确响应时效、服务渠道、巡检与故障升级机制。"),
            ("质保期与备件承诺", "说明质保期限、备件供应与质保期内服务边界。"),
        ),
    ),
    (
        "报价部分",
        0.5,
        (
            ("开标一览表", "按招标文件格式填报总价与关键商务信息。"),
            ("分项报价表", "按采购内容拆分报价，说明计价口径。"),
            ("报价说明", "说明报价包含范围、税费与不可预见费处理方式。"),
        ),
    ),
)


def standard_plan() -> list[SectionPlan]:
    plans: list[SectionPlan] = []
    for group, weight, children in STANDARD_TEMPLATE:
        plans.append(SectionPlan(heading=group, intent=f"{group}总体说明与下级章节分工。", level=1, weight=weight * 0.45))
        for heading, intent in children:
            plans.append(SectionPlan(heading=heading, intent=intent, level=2, weight=weight))
    return plans


def _clean_heading(title: str) -> str:
    text = re.sub(r"[（(][^）)]{0,24}[）)]", "", str(title or ""))
    text = re.split(r"[（(]", text)[0]
    return text.strip(" 　\t、，。：:《》\"“”")


def tender_plan(corpus: str) -> list[SectionPlan]:
    """从招标文件的「投标文件格式」章节抽取骨架，失败时返回空列表。

    优先按「第X章」的正式格式清单出顶层章；只有清单不存在时才退回「一、二、三」条目。
    顶层章名与顺序完全照抄招标文件，不增、不删、不改名。
    """
    lines = [line.strip() for line in (corpus or "").splitlines()]
    start = -1
    for index, line in enumerate(lines):
        if len(line) <= 40 and FORMAT_HINT.search(line):
            start = index
            break
    if start < 0:
        return []

    chapter_titles: list[str] = []
    cn_titles: list[str] = []
    seen: set[str] = set()
    for raw in lines[start + 1 : start + 300]:
        line = raw.strip()
        if not line or len(line) > 60:
            continue
        chapter = CHAPTER_LINE.match(line)
        if chapter:
            title = _clean_heading(chapter.group(2))
            if title and len(title) >= 2 and not FLOW_TERMS.search(title) and title not in seen:
                seen.add(title)
                chapter_titles.append(title)
            continue
        # 格式清单结束：遇到下一个一级中文条目（如「六、需提交的材料」）就停止，
        # 否则材料清单、实质性要求会被误当成投标文件章节。
        if chapter_titles and CN_SECTION.match(line):
            break
        section = CN_SECTION.match(line) or PAREN_SECTION.match(line)
        if section and not chapter_titles:
            title = _clean_heading(section.group(2))
            if title and len(title) >= 2 and not FLOW_TERMS.search(title) and title not in seen:
                seen.add(title)
                cn_titles.append(title)

    titles = chapter_titles or cn_titles
    if len(titles) < 3:
        return []

    plans: list[SectionPlan] = []
    for title in titles[:20]:
        plans.append(
            SectionPlan(
                heading=title,
                intent=f"按招标文件规定的格式与内容编写「{title}」，逐条响应相关要求。",
                level=1,
                weight=_template_weight(title),
            )
        )
        for child in _TEMPLATE_CHILDREN.get(title, ()):
            plans.append(
                SectionPlan(
                    heading=child,
                    intent=f"在「{title}」项下编写「{child}」，响应招标文件与评分标准。",
                    level=2,
                    weight=_template_weight(title) * 0.9,
                )
            )
    return plans


def _template_weight(title: str) -> float:
    return 3.0 if "技术" in title else 2.0 if "服务" in title else 0.8


def expand_template_scores(plans: list[SectionPlan], score_items: list[dict]) -> list[SectionPlan]:
    """把评分项挂到对应的模板章下；模板顶层章名与顺序保持不变。"""
    if not score_items:
        return plans
    result: list[SectionPlan] = []
    for plan in plans:
        result.append(plan)
    tech_parent = next((p for p in plans if p.level == 1 and "技术方案" in p.heading), None)
    service_parent = next((p for p in plans if p.level == 1 and "服务" in p.heading), None)
    existing = {plan.heading for plan in plans}
    for item in score_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 24 or name in existing:
            continue
        parent = None
        if tech_parent and _TECH_SCORE.search(name):
            parent = tech_parent
        elif service_parent and _SERVICE_SCORE.search(name):
            parent = service_parent
        if not parent:
            continue
        # 「服务保障」「技术方案」这种评分部分名本身不是新章节：它已经有对应的模板章，
        # 再补一个同名一级章会把招标原件规定的 9 章变成 10 章。
        if name in parent.heading or parent.heading in name:
            continue
        result.append(
            SectionPlan(
                heading=name,
                intent=f"响应评分项「{name}」，覆盖评分标准中的全部要点。",
                level=2,
                weight=parent.weight * 0.9,
            )
        )
        existing.add(name)
    return result


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[\W_]+", "", text or "")
    return {clean[index : index + 2] for index in range(max(0, len(clean) - 1))}


def match_scores(plans: list[SectionPlan], scoring_items: list[dict]) -> None:
    if not scoring_items:
        return
    for plan in plans:
        grams = _bigrams(plan.heading + " " + plan.intent)
        ranked = []
        for item in scoring_items:
            name = str(item.get("name") or "")
            criteria = str(item.get("criteria") or "")
            score = len(grams & _bigrams(name + " " + criteria))
            ranked.append((score, name, item))
        ranked.sort(key=lambda row: row[0], reverse=True)
        matched = [name for score, name, _ in ranked[:2] if score > 0 and name]
        plan.scores = matched
        if matched:
            points = 0.0
            for _, name, item in ranked[:2]:
                if name in matched:
                    try:
                        points += float(item.get("points") or 0)
                    except (TypeError, ValueError):
                        pass
            plan.weight = max(plan.weight, 1.0 + points / 25)
            if not plan.intent:
                plan.intent = f"围绕评分项「{'、'.join(matched)}」组织本节内容。"


def assign_targets(plans: list[SectionPlan], total_words: int) -> None:
    total_words = max(10000, min(300000, int(total_words or 80000)))
    total_weight = sum(max(0.1, plan.weight) for plan in plans) or 1
    remaining = total_words
    for index, plan in enumerate(plans):
        if index == len(plans) - 1:
            words = remaining
        else:
            words = int(total_words * max(0.1, plan.weight) / total_weight)
            remaining -= words
        plan.target_words = max(300, min(8000, words))


def build_outline(mode: str, corpus: str = "") -> tuple[list[SectionPlan], str]:
    if mode == "tender":
        plans = tender_plan(corpus)
        if plans:
            return plans, "按招标文件投标文件格式章生成"
    return standard_plan(), "标准投标文件结构"
