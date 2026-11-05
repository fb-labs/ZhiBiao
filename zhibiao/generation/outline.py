from __future__ import annotations

import re
from dataclasses import dataclass, field

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


def tender_plan(corpus: str) -> list[SectionPlan]:
    """从招标文件的「投标文件格式」章抽取骨架，失败时返回空列表。

    解析统一交给 core.tender_outline：先定位真正的格式章，再按原件目录抽出
    每个部分；技术/服务部分按供货要求章的小节展开。

    旧实现从正文里第一个「投标文件格式」字样往后扫 300 行：那一处常常只是
    「投标人须知 → 招标文件的组成 →（6）投标文件格式；」这种引用，真正的
    第六章在后面几千字之外，扫描早就结束，于是静默返回空列表。调用方又只看
    plans 长度（空列表回退成 28 章标准结构，同样 >= 3），把标准 IT 结构当成
    「原件目录」，测试用例1 的 11 个格式部分就是这样整份丢掉的。
    """
    from zhibiao.core.tender_outline import outline_from_tender

    chapters, _note = outline_from_tender(corpus)
    if len(chapters) < 3:
        return []

    plans: list[SectionPlan] = []
    parent_title = ""
    parent_weight = 0.8
    for level, title in chapters[:40]:
        if level <= 1:
            parent_title = title
            parent_weight = _template_weight(title)
            plans.append(
                SectionPlan(
                    heading=title,
                    intent=f"按招标文件规定的格式与内容编写「{title}」，逐条响应相关要求。",
                    level=1,
                    weight=parent_weight,
                )
            )
        else:
            plans.append(
                SectionPlan(
                    heading=title,
                    intent=f"在「{parent_title}」项下编写「{title}」，响应招标文件与评分标准。",
                    level=2,
                    weight=parent_weight * 0.9,
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
        new_plan = SectionPlan(
            heading=name,
            intent=f"响应评分项「{name}」，覆盖评分标准中的全部要点。",
            level=2,
            weight=parent.weight * 0.9,
        )
        # 不能统一 append 到列表末尾：落库时二级章节的 parentKey 取「最近的一级章」，
        # 末尾追加会把它挂到最后那个一级章（实测「技术服务和质保期服务能力」挂到了
        # 「其他资料」）。插到父章现有子章之后，层级与顺序才和大纲一致。
        insert_at = len(result)
        for index, plan in enumerate(result):
            if plan is parent:
                insert_at = index + 1
                while insert_at < len(result) and result[insert_at].level > 1:
                    insert_at += 1
                break
        result.insert(insert_at, new_plan)
        existing.add(name)
    return result


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[\W_]+", "", text or "")
    return {clean[index : index + 2] for index in range(max(0, len(clean) - 1))}


# 这些二字词在任何章节/评分项里都会撞上，不能作为「对齐」的依据。
# 旧实现把评分标准(criteria)和 intent 一起算并接受 1 次重叠，结果「投标函」
# 同时命中「投标设备整体评价」和「投标人业绩」——只因双方都有「投标」两个字。
_GENERIC_GRAMS = {
    "投标", "招标", "文件", "项目", "采购", "内容", "相关", "其他", "以及",
    "进行", "提供", "说明", "编制", "评价", "能力", "程度",
}


def match_scores(plans: list[SectionPlan], scoring_items: list[dict]) -> None:
    if not scoring_items:
        return
    parent = ""
    for plan in plans:
        if plan.level <= 1:
            parent = plan.heading
            context = plan.heading
        else:
            # 二级章节的标题常常是「技术参数」这种短词，带上父章标题才有语义；
            # 但**不能用 intent**：它是「逐条响应相关要求」这类模板话术，
            # 「响应」「要求」会把所有章节都拉到同一个评分项上。
            context = f"{plan.heading} {parent}"
        grams = _bigrams(context) - _GENERIC_GRAMS
        ranked = []
        for item in scoring_items:
            name = str(item.get("name") or "")
            overlap = len(grams & (_bigrams(name) - _GENERIC_GRAMS))
            ranked.append((overlap, name, item))
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
