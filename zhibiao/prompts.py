from __future__ import annotations

ANALYSIS_SYSTEM = """你是资深招投标顾问，擅长从招标文件中提炼关键约束与评审规则。
只依据提供的招标文件正文作答，不要编造未出现的信息；无法确认的内容写“未明确”。
必须返回一个 JSON 对象，不要输出任何额外说明。"""

ANALYSIS_SCHEMA = """请严格按以下 JSON 结构输出：
{
  "overview": "200字以内的项目总览，说明采购背景、建设内容与投标要点",
  "dimensions": [
    {"key": "project", "label": "项目概况", "value": "一句话结论", "note": "依据原文的补充说明"},
    {"key": "buyer", "label": "采购人", "value": "...", "note": "..."},
    {"key": "budget", "label": "预算金额", "value": "...", "note": "..."},
    {"key": "deadline", "label": "投标截止", "value": "...", "note": "..."},
    {"key": "deposit", "label": "投标保证金", "value": "...", "note": "..."},
    {"key": "qualification", "label": "资格要求", "value": "...", "note": "..."},
    {"key": "delivery", "label": "交付要求", "value": "...", "note": "..."},
    {"key": "scoring", "label": "评分办法", "value": "...", "note": "..."}
  ],
  "scoring": {
    "method": "综合评分法/最低价法等",
    "total": 100,
    "items": [{"name": "评分项", "points": 10, "criteria": "得分条件"}]
  },
  "risks": [
    {"level": "blocker|critical|major|minor|info", "title": "风险标题", "detail": "可能导致的后果与应对建议"}
  ],
  "requirements": ["必须逐条响应的实质性要求，每条一句话"]
}
dimensions 必须覆盖以上 8 项；requirements 至少 5 条，无法从原文确认时不要编造。"""


def analysis_messages(title: str, buyer: str, corpus: str) -> list[dict]:
    user = f"""项目名称：{title}
采购人：{buyer or '未填写'}

招标文件正文（已截断）：
{corpus}

{ANALYSIS_SCHEMA}"""
    return [
        {"role": "system", "content": ANALYSIS_SYSTEM},
        {"role": "user", "content": user},
    ]


OUTLINE_SYSTEM = """你是投标文件结构设计专家，熟悉政府采购与工程招标的响应文件编排规则。
依据招标解读结果设计投标文件目录，章节要覆盖资格响应、商务响应、技术方案、服务保障、报价与承诺。
只返回 JSON 对象。"""

OUTLINE_SCHEMA = """请严格按以下 JSON 结构输出：
{
  "sections": [
    {
      "heading": "一级章节标题（如：投标函及商务响应）",
      "intent": "该章节要证明什么、对应哪条评分项",
      "targetWords": 800,
      "children": [
        {"heading": "二级标题", "intent": "写作要点", "targetWords": 600, "children": []}
      ]
    }
  ]
}
一级章节 6-10 个，最多两级；targetWords 为整数，合计约 8000-15000 字。"""


def outline_messages(title: str, analysis: dict) -> list[dict]:
    user = f"""项目名称：{title}
项目总览：{analysis.get('overview', '')}
关键维度：{analysis.get('dimensions', [])}
评分办法：{analysis.get('scoring', {})}
实质性要求：{analysis.get('requirements', [])}

{OUTLINE_SCHEMA}"""
    return [
        {"role": "system", "content": OUTLINE_SYSTEM},
        {"role": "user", "content": user},
    ]


WRITE_SYSTEM = """你是投标文件撰写专家，文字严谨、务实、有说服力。
按给定章节意图撰写正文，逐条响应招标要求，使用小标题、清单与量化承诺；
不要编造具体案例、证书编号或未经提供的业绩；不要输出与章节无关的内容。
使用 Markdown 正文（可用 ## 小标题、列表），不要重复章节标题本身。"""


def write_messages(
    tender_title: str,
    heading: str,
    intent: str,
    target_words: int,
    context: str,
    analysis_brief: str,
) -> list[dict]:
    user = f"""项目名称：{tender_title}
当前章节：{heading}
写作意图：{intent or '按招标要求完整响应'}
目标字数：约 {target_words} 字
项目解读摘要：{analysis_brief}

已完成的相邻章节摘要（用于衔接，不要重复内容）：
{context or '（无）'}

请直接输出该章节正文。"""
    return [
        {"role": "system", "content": WRITE_SYSTEM},
        {"role": "user", "content": user},
    ]


REVIEW_SYSTEM = """你是评标专家，按招标文件的资格条件、评分办法与响应性要求审查投标文件。
只指出可核实的问题，给出可执行的修改建议；语气客观，不评价投标人主观意图。
只返回 JSON 对象。"""

REVIEW_SCHEMA = """请严格按以下 JSON 结构输出：
{
  "score": 0,
  "verdict": "可通过|需修改|不可提交",
  "summary": "100字以内总体结论",
  "issues": [
    {
      "severity": "blocker|critical|major|minor|info",
      "category": "完整性|响应性|一致性|合规性|表达",
      "location": "章节标题或位置",
      "finding": "发现的问题",
      "recommendation": "修改建议"
    }
  ]
}
score 为 0-100 的整数；问题按严重程度排序；没有问题时 issues 返回空数组。"""


def review_messages(tender_title: str, analysis_brief: str, document_text: str) -> list[dict]:
    user = f"""项目名称：{tender_title}
招标解读摘要：{analysis_brief}

投标文件正文：
{document_text}

{REVIEW_SCHEMA}"""
    return [
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user", "content": user},
    ]


FIX_SYSTEM = """你是投标文件改写专家。根据审查意见重写指定章节，保留原有有效内容，
补齐缺失的响应点，语言更具体，不要编造资质或业绩。只输出重写后的章节正文。"""


def fix_messages(heading: str, finding: str, recommendation: str, body: str) -> list[dict]:
    user = f"""章节：{heading}
审查问题：{finding}
修改建议：{recommendation}

原章节正文：
{body}

请输出修改后的完整章节正文（Markdown），不要附加解释。"""
    return [
        {"role": "system", "content": FIX_SYSTEM},
        {"role": "user", "content": user},
    ]
