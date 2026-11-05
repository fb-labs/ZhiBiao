"""Build a deterministic project fact and commitment ledger from stored analysis.

设计要点：
- 全局事实未完成时**直接拒绝生成**（「请先完成全局事实设定，再生成正文」），
  而不是带着空事实硬写
- 80 端口 `generate.py:3271,3282`：全局事实缺失只 `logger.warning` 就继续 ——
  这项缺口 3001 选择补齐

本模块保持 `build_project_ledger` 的原有签名与行为不变（避免回归），另加：
  - `confirmed_dim_facts`  ：只统计来自招标文件解读的已确认事实
  - `facts_ready`          ：生成前的准入判断（缺事实即拒绝）
  - `build_chapter_ledger` ：把产出侧事实台账渲染成文本，便于审计与报告
  - `truncate_ledger`      ：按「【…】」小节保留地截断台账，编排阶段不会整段丢掉规格书要点
"""

from __future__ import annotations

import re
from typing import Any


def confirmed_dim_facts(dims: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """只统计来自招标文件解读的已确认事实（不含「项目名称」这条自动项）。

    「已确认」= 解读结果里 hit 为真、且名称与取值都非空。
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in dims or []:
        if not isinstance(item, dict) or not item.get("hit"):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        key = (name, value)
        if name and value and key not in seen:
            out.append(key)
            seen.add(key)
    return out


def facts_ready(project_name: str, dims: list[dict[str, Any]]) -> tuple[bool, str]:
    """生成前的准入判断。返回 (是否可生成, 原因)。

    生成准入要求「请先完成全局事实设定，再生成正文」：招标文件还没解读出任何可核查事实
    时，正文只能写空话，且模型在缺事实约束下极易编造专利号/业绩 —— 那正是废标级风险。
    """
    confirmed = confirmed_dim_facts(dims)
    if confirmed:
        return True, ""
    if str(project_name or "").strip():
        return False, (
            "尚未从招标文件解读中确认任何项目事实（编号、期限、地点、质保、付款等），"
            "只有一条项目名称。此时正文只能写空话，且模型极易编造专利号、业绩和企业资质。"
            "请先在「招标解读」完成解读并核对抽取结果，再生成正文。"
        )
    return False, (
        "项目还没有上传并解读招标文件，无法确定任何可核查事实。"
        "请先上传招标文件并完成「招标解读」，再生成正文。"
    )


def build_project_ledger(
    project_name: str,
    dims: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    *,
    spec_digest: str = "",
    param_lock: str = "",
    industry_text: str = "",
    tender_text: str = "",
) -> tuple[list[tuple[str, str]], str]:
    facts: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in dims or []:
        if not isinstance(item, dict) or not item.get("hit"):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        key = (name, value)
        if name and value and key not in seen:
            facts.append(key)
            seen.add(key)
    fact_lines = [f"- {name}：{value}" for name, value in facts]
    # 先过滤掉非法/空名条目再切片：原实现 scores[:40] 先切片后过滤，第 41 条真评分项
    # 会被静默丢弃（台账里看不到，编排与正文都少了一条评分口径）。
    score_items = [
        item for item in (scores or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    score_lines: list[str] = []
    for item in score_items[:40]:
        name = str(item.get("name") or "").strip()
        raw_score = item.get("score")
        text_score = "" if raw_score is None else str(raw_score).strip()
        # 缺分时不能渲染成「None分」（导出/审计都看不懂），统一写「未标注」
        line = f"- {name}：{text_score}分" if text_score else f"- {name}：未标注"
        criteria = str(item.get("criteria") or "").strip()
        if criteria:
            line += f"；评分标准：{criteria[:240]}"
        score_lines.append(line)
    lock_text = str(param_lock or "").strip()
    # 技术要求条目 / 边界条款 / 部件参数块：逐条响应与「由矿方自备」这类供货边界
    # 都不能靠模型自己回忆。实测漏掉第 15 条「皮带变频开关由矿方自备」，全文零次出现；
    # 变频器的 ExdIMb/400kW/1140V/50Hz/IP54/水冷/S1 也只写了一句「隔爆兼本安型」。
    from zhibiao.core.completeness import boundary_clauses, component_blocks, tech_items
    _items = [item for item in tech_items(tender_text)] if tender_text else []
    _boundary = boundary_clauses(tender_text) if tender_text else []
    _blocks = component_blocks(tender_text) if tender_text else []
    items_text = ""
    if _items:
        _lines = []
        for item in _items:
            mark = "（供货边界：按招标原文核对责任主体及我方供货范围）" if item["boundary"] else ""
            _lines.append(f"- 第{item['index']}条 {item['head']}{mark}")
        items_text = "\n".join(_lines)
    block_text = ""
    if _blocks:
        block_text = "\n".join(
            f"- {block['name']}：" + "；".join(f"{k}{v}" for k, v in block["params"].items())
            for block in _blocks)
    ledger = (
        ("【唯一参数口径（最高优先级，必须原样引用；不得改写、不得用「按技术协议执行」替代）】\n"
         + lock_text + "\n\n" if lock_text else "")
        + "【项目事实】\n" + ("\n".join(fact_lines) or "（尚无已确认事实）")
        + "\n项目名称只能使用上述已确认的招标项目名称；未确认时写{{待填:招标项目名称}}，不得将平台内部项目名当作招标项目名称。"
        + "\n\n【评分项口径】\n" + ("\n".join(score_lines) or "（尚未抽取评分项）")
        + ("\n\n【招标技术要求条目（逐条响应，一条都不能少；对照表的行数不得少于条目数）】\n"
           + items_text if items_text else "")
        + ("\n\n【部件参数块（原文口径，涉及该部件时必须按其参数写全，不得只写「隔爆兼本安型」这类一个词）】\n"
           + block_text if block_text else "")
        + "\n\n【承诺口径规则】\n"
        "招标文件已明确的数值、对象、单位、期限和起算条件按原口径写；不同对象或不同起算条件的事实分别保留，"
        "不得互相替换。没有来源的价格、证书编号、企业业绩、人员姓名、专利号和企业资质一律写为"
        "{{待填:具体信息}}。不把计划性措施写成企业已经具备的事实，也不新增无依据的量化承诺。\n"
        "联系人口径：招标文件里的联系人、电话只用于澄清、答疑、递交等程序性表述；"
        "严禁把招标代理机构或平台客服的联系人、电话写成招标人/采购人/使用单位的现场负责人、"
        "生产调度或应急抢修电话。招标人联系人、电话、现场负责人一律不得留待填："
        "原文已给出的（见「联系方式」等章节）直接引用，原文未给出的写「由招标人指定」或省略；"
        "只有投标人自己的人员、证书、业绩、报价缺失时才写 {{待填:投标人现场负责人及电话}}。"
        "\n表格与占位口径："
        "（1）表格每一行的单元格数必须与表头一致，不得少列、并列或留空单元格，写到一半断开必须补完整；"
        "（2）对照表中「作用域/适用对象」列写适用对象与层级（单台整机、单台部件），"
        "「证明材料来源」列写可核查的证明材料，不得用「无偏离」「完全响应」代替；"
        "（3）数量口径：单台配置数量与项目总量必须分别写明，"
        "招标原文未给出数量的部件不得自行填数量（写「随主机配套」或 {{待填:数量}}），"
        "同一部件前后数量不得矛盾；"
        "（4）需要插图的位置只写一行 {{图片占位:具体图名与盖章/截图要求}}，"
        "说明必须写清「插什么、要不要盖章、要不要截全」（如「营业执照副本扫描件（加盖投标人公章）」"
        "「信誉查询网页截图（信用中国，截全网址与查询日期）」），不得只写「请插入图片」或「示意图」；"
        "不要用方括号自行编造【一】【二】这类填空；"
        "（5）招标文件合同模板里的【一】【　】等空白填空不得原样抄进正文，"
        "原文没有的数值一律写 {{待填:字段名}}（导出时自动标红）。"
        + ("\n\n【行业口径（必须遵守）】\n" + industry_text if industry_text else "")
        + ("\n\n【技术规格书要点（招标文件原文摘录，正文必须引用其中的真实参数与标准号，"
           "不得再写「待填」）】\n" + spec_digest if spec_digest else "")
    )
    return facts, ledger


def build_chapter_ledger(ledger_rows: list[dict[str, Any]]) -> str:
    """把产出侧事实台账（`extract_chapter_facts` 的结果）渲染成人类可读文本。"""
    lines: list[str] = []
    for row in ledger_rows or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        # 空字段直接跳过：原实现会渲染出「None=None」，审计文本里是纯噪声
        parts: list[str] = []
        for item in row.get("facts") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            if label and value:
                parts.append(f"{label}={value}")
        if not parts:
            continue
        lines.append(f"- 第{row.get('seq')}节《{title}》：{'；'.join(parts)}")
    return "\n".join(lines)


# 台账小节标题形如「【唯一参数口径（…）】」/「【技术规格书要点（…）】」，都在行首。
_LEDGER_SECTION_RE = re.compile(r"(?m)^(?=【)")
# 每节最少保留的字符数（保证小节标题 + 开头可见，不会整节消失）
_LEDGER_MIN_SECTION_CHARS = 120


def split_ledger_sections(text: str) -> list[str]:
    """按行首「【…】」把台账切成小节，保留原文顺序。"""
    value = str(text or "")
    if not value:
        return []
    parts = [part for part in _LEDGER_SECTION_RE.split(value) if part]
    return parts or [value]


def truncate_ledger(text: str, max_chars: int = 12000) -> str:
    """按小节保留地截断台账，保证每个「【…】」小节都有配额。

    台账是拼接出来的：参数口径最前、技术规格书要点最后。调用方原先直接 `[:12000]`，
    当「唯一参数口径」很长（实测 13937 字）时会把**末尾的【技术规格书要点】整节丢掉**，
    编排模型看不到真实参数与标准号，各节写作重点只能空泛。这里先给每节一个最小配额，
    再把剩余预算按「最长的节优先」分下去，任何一节都不会被整段丢弃。
    """
    value = str(text or "")
    budget = int(max_chars or 0)
    if budget <= 0:
        return ""
    if len(value) <= budget:
        return value
    sections = split_ledger_sections(value)
    if len(sections) <= 1:
        return value[:budget]
    if budget <= len(sections):
        # 预算小到每节连 1 个字符都放不下，退回整体截断，保证长度不超预算
        return value[:budget]
    quota = max(_LEDGER_MIN_SECTION_CHARS, budget // (len(sections) * 2))
    quota = min(quota, budget // len(sections))
    alloc = [min(len(section), quota) for section in sections]
    remaining = budget - sum(alloc)
    # 最长的小节优先补足；每轮在「还没取满的小节」之间均分，直到预算用尽或全部完整保留。
    order = sorted(range(len(sections)), key=lambda i: -len(sections[i]))
    while remaining > 0:
        pending = [i for i in order if alloc[i] < len(sections[i])]
        if not pending:
            break
        share = max(1, remaining // len(pending))
        for i in pending:
            if remaining <= 0:
                break
            add = min(len(sections[i]) - alloc[i], share, remaining)
            if add > 0:
                alloc[i] += add
                remaining -= add
    return "".join(section[:alloc[i]] for i, section in enumerate(sections))
