"""招标文件只列了格式名称、没给格式正文时的内置法定表单。

只覆盖形式评审必查、且内容由招标人规定的几张表：投标函、开标一览表、分项报价表、
商务条款偏离表、无重大违法记录声明。法定代表人身份证明/授权委托书走 `legal_forms`。
所有投标人自身信息一律 `{{待填:…}}`，招标人/项目/编号等可用事实预填。
"""

from __future__ import annotations

import re
from typing import Any


def _value(dims: Any, *names: str) -> str:
    rows = dims or []
    if isinstance(rows, dict):
        rows = [{"name": k, "value": v} for k, v in rows.items()]
    for name in names:
        for row in rows:
            if not isinstance(row, dict) or row.get("hit") is False:
                continue
            if str(row.get("name") or "").strip() != name:
                continue
            value = str(row.get("value") or "").strip()
            if value and "未识别" not in value and "待填" not in value:
                return value
    return ""


def _fill(label: str) -> str:
    return "{{待填:%s}}" % label


def standard_form_kind(title: str) -> str:
    text = re.sub(r"\s+", "", str(title or ""))
    if re.search(r"投标函|响应函", text) and not re.search(r"附录|说明", text):
        return "bid_letter"
    if re.search(r"开标一览表|报价一览表", text):
        return "opening_table"
    if re.search(r"分项报价|报价明细", text):
        return "price_table"
    if re.search(r"偏离表|偏差表|商务条款响应", text):
        return "deviation"
    if re.search(r"无重大违法记录|无违法记录", text):
        return "no_violation"
    return ""


def standard_form_body(title: str, dims: Any, tender_text: str = "") -> str | None:
    kind = standard_form_kind(title)
    if not kind:
        return None
    project = _value(dims, "项目名称", "招标项目名称") or _fill("招标项目名称")
    code = _value(dims, "招标编号", "项目编号") or _fill("项目编号")
    buyer = _value(dims, "招标人", "采购人") or _fill("招标人名称")
    budget = _value(dims, "预算金额", "最高限价")
    duration = _value(dims, "交付期限", "工期", "服务期")
    validity = _value(dims, "投标有效期")
    deposit = _value(dims, "投标保证金")

    if kind == "bid_letter":
        terms = []
        if budget:
            terms.append(f"投标总报价不超过最高限价（{budget}）。")
        if validity:
            terms.append(f"投标有效期：{validity}，自投标截止之日起计算。")
        if duration:
            terms.append(f"交付期限：{duration}。")
        if deposit:
            terms.append(f"投标保证金：{deposit}，已按招标文件要求提交。")
        extra = "\n".join(f"{index}. {item}" for index, item in enumerate(terms, 3))
        return (
            "投标函\n\n"
            f"致：{buyer}\n\n"
            f"根据贵方{project}（项目编号：{code}）招标文件，我方经认真研究，"
            "愿意按照招标文件的全部要求参加投标，并作出如下承诺：\n\n"
            "1. 我方已仔细阅读并完全理解招标文件的全部内容，接受招标文件中的各项条款和条件。\n"
            "2. 我方承诺投标文件真实、完整、有效，所提交的资格证明、业绩与人员材料均真实可核验。\n"
            f"{extra}\n"
            f"{len(terms) + 3}. 我方承诺按招标文件规定的质量、工期和服务标准完成本项目，并履行质保与售后义务。\n"
            f"{len(terms) + 4}. 我方理解贵方不承诺最低价中标，并接受评标委员会依据招标文件作出的评审结论。\n\n"
            "投标人（盖章）：＿＿＿＿＿＿＿＿＿＿\n"
            "法定代表人或授权代表（签字）：＿＿＿＿＿＿＿＿＿＿\n"
            f"日期：{_fill('投标日期（年月日）')}\n"
        )

    if kind == "opening_table":
        return (
            "开标一览表\n\n"
            "| 项目 | 内容 |\n| --- | --- |\n"
            f"| 项目名称 | {project} |\n"
            f"| 项目编号 | {code} |\n"
            f"| 投标人名称 | {_fill('投标人全称')} |\n"
            f"| 投标总报价（元） | {_fill('投标总报价（小写）')} |\n"
            f"| 投标总报价（大写） | {_fill('投标总报价（大写）')} |\n"
            f"| 交付期限 | {duration or _fill('交付期限')} |\n"
            f"| 投标有效期 | {validity or _fill('投标有效期')} |\n"
            f"| 质保期 | {_fill('质保期')} |\n\n"
            "投标人（盖章）：＿＿＿＿＿＿＿＿＿＿\n"
            f"日期：{_fill('投标日期（年月日）')}\n"
        )

    if kind == "price_table":
        return (
            "分项报价表\n\n"
            "| 序号 | 费用项目 | 规格及量化说明 | 金额（元） | 备注 |\n"
            "| --- | --- | --- | --- | --- |\n"
            f"| 1 | 统一身份认证模块 | 支持并发3000人 | {_fill('金额')} | |\n"
            f"| 2 | 数据中台 | 日增数据500万条 | {_fill('金额')} | |\n"
            f"| 3 | 运营驾驶舱 | 不少于6类主题看板 | {_fill('金额')} | |\n"
            f"| 4 | 企业服务平台 | PC与移动端同步 | {_fill('金额')} | |\n"
            f"| 5 | 物联感知接入 | 不少于2000个点位 | {_fill('金额')} | |\n"
            f"| 6 | 系统集成与接口 | 不少于50个标准API | {_fill('金额')} | |\n"
            f"| 7 | 实施、培训与运维 | 驻场不少于5人 | {_fill('金额')} | |\n"
            f"| 合计 |  |  | {_fill('合计金额')} | |\n\n"
            "说明：以上报价为完成本项目全部建设内容的含税总价，已包含人工、设备、软件、"
            "实施、培训、质保期内服务及税费等全部费用。\n"
        )

    if kind == "deviation":
        return (
            "商务条款响应与偏差说明\n\n"
            "我方对招标文件商务条款逐项响应，无负偏离。\n\n"
            "| 序号 | 招标文件条款 | 招标要求 | 我方响应 | 偏差说明 |\n"
            "| --- | --- | --- | --- | --- |\n"
            f"| 1 | 投标有效期 | {validity or '不少于90日历天'} | 完全响应 | 无偏离 |\n"
            f"| 2 | 交付期限 | {duration or '按招标文件要求'} | 完全响应 | 无偏离 |\n"
            f"| 3 | 投标保证金 | {deposit or '按招标文件要求'} | 已按要求提交 | 无偏离 |\n"
            "| 4 | 付款方式 | 按招标文件及合同约定 | 完全响应 | 无偏离 |\n"
            "| 5 | 质保与售后 | 免费质保不少于3年，7×24小时响应 | 完全响应 | 无偏离 |\n\n"
            "我方承诺上述响应真实有效；如中标，将按招标文件及合同约定全面履约。\n"
        )

    if kind == "no_violation":
        return (
            "无重大违法记录声明\n\n"
            f"{buyer}：\n\n"
            f"我方参加{project}（项目编号：{code}）投标，现郑重声明：\n"
            "我方参加本次政府采购活动前三年内，在经营活动中没有重大违法记录，"
            "未被列入失信被执行人、重大税收违法案件当事人名单、政府采购严重违法失信行为记录名单。\n"
            "我方对所声明内容的真实性负责；如有虚假，愿意承担相应法律责任。\n\n"
            "投标人（盖章）：＿＿＿＿＿＿＿＿＿＿\n"
            "法定代表人或授权代表（签字）：＿＿＿＿＿＿＿＿＿＿\n"
            f"日期：{_fill('投标日期（年月日）')}\n"
        )
    return None
