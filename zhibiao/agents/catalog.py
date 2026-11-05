from __future__ import annotations

from zhibiao.agents.base import AgentSpec
from zhibiao.agents.tools import TOOLS

FACT_SCHEMA = """{
  "project": {"name":"","code":"","buyer":"","budget":"","deadline":"","bidValidity":"","duration":"","deposit":""},
  "bidder": {"name":"","aliases":[]},
  "materials": [{"name":"投标函","note":"招标文件要求的文件或材料"}],
  "parameters": [{"name":"参数名称","required":"招标文件要求值","unit":"单位"}],
  "qualifications": ["资格条件原文要点"],
  "commitments": ["必须承诺或响应的实质性要求"],
  "pricing": {"maxPrice":"最高限价或预算","currency":"人民币"},
  "signature": {"rule":"签字盖章要求"},
  "copies": {"rule":"正副本份数与封装要求"},
  "template": {"modeHint":"generic|docx_template|pdf_template","chapters":["模板章节"]},
  "manualChecklist": ["需要人工核对的封装/原件/现场事项"]
}"""


TENDER_READER = AgentSpec(
    key="tender_reader",
    name="招标解析 Agent",
    description="从招标文件原文抽取项目身份、材料清单、技术参数、资格条件、承诺要求与模板信息，形成可复核的事实台账。",
    system_prompt=f"""工作目标：
1. 输入参数中已包含 tenderDigest（压缩后的招标要点）。直接据此抽取事实，不要先调用 tender_digest；只有关键条款缺失时才用 search_tender 或 read_tender(limit=6000) 定点补充。
2. 只抽取原文明确出现的信息；没有写的字段留空，不得猜测。
3. 项目名称、项目编号、采购人、预算/最高限价、投标截止、投标有效期、总工期、投标保证金必须尽量抽取，并保留原文口径。
4. materials 列出招标文件要求提交的全部文件与材料（投标函、授权委托书、开标一览表、报价表、资格证明、保证金回单、电子版等）。
5. parameters 抽取技术参数/材料清单/数量/标准号；required 写招标文件要求值，不要改写。
6. qualifications 抽取资格条件；commitments 抽取实质性响应与承诺要求。
7. template.chapters 如招标文件规定了投标文件格式章节，按原顺序列出；modeHint 取 generic/docx_template/pdf_template。
8. manualChecklist 写下无法自动核对的签字、盖章、封装、原件携带等事项。
9. 调用 save_facts 保存事实，facts 参数必须严格符合下面结构：
{FACT_SCHEMA}
10. 保存成功后调用 finish，返回保存的事实摘要。""",
    tools=[
        TOOLS["read_tender"],
        TOOLS["search_tender"],
        TOOLS["read_analysis"],
        TOOLS["save_facts"],
        TOOLS["read_facts"],
    ],
    max_steps=8,
    assistant_key="analyst",
)


PLANNER = AgentSpec(
    key="planner",
    name="生成规划 Agent",
    description="根据招标事实与模板情况，在通用、DOCX 模板、PDF 模板三种生成模式中选择一种，并生成可写作的大纲。",
    system_prompt="""工作目标：
1. 先 read_facts；若没有事实，先提示需要运行招标解析 Agent。
2. 调用 detect_mode 判断生成模式；调用 extract_template 获取模板章节。
3. 根据模式调用 build_outline：mode 传 generic/docx_template/pdf_template，targetWords 使用输入参数（默认 80000）。
4. 大纲必须覆盖评分项、资格响应、商务响应、技术方案、服务保障与报价；模板模式要覆盖模板章节。
5. 完成后 finish，返回 mode、sections 数量、targetWords 与说明。
不得编造章节或事实。""",
    tools=[
        TOOLS["read_facts"],
        TOOLS["detect_mode"],
        TOOLS["extract_template"],
        TOOLS["build_outline"],
        TOOLS["list_sections"],
    ],
    max_steps=6,
    assistant_key="outliner",
)


BUILDER = AgentSpec(
    key="builder",
    name="正文编制 Agent",
    description="按大纲与招标事实组织正文生成，调用现有生成引擎逐章写作，并在完成后做一次生成后检查。",
    system_prompt="""工作目标：
1. 先 read_facts 与 list_sections，确认大纲与事实是否就绪。
2. 如果没有章节，先 detect_mode 并 build_outline。
3. 调用 generate_missing：输入 mode 使用 payload.mode（missing/retry/all，默认 missing）。
4. 生成后调用 audit_document，记录问题数量。
5. finish，返回 generated/failed/quality/issues 摘要。
不得跳过事实核对，不得编造生成结果。""",
    tools=[
        TOOLS["read_facts"],
        TOOLS["detect_mode"],
        TOOLS["build_outline"],
        TOOLS["list_sections"],
        TOOLS["generate_missing"],
        TOOLS["audit_document"],
    ],
    max_steps=6,
    assistant_key="writer",
)


AUDITOR = AgentSpec(
    key="auditor",
    name="核对 Agent",
    description="按招标文件原文事实核对成稿：项目名称与编号、投标人身份、材料、参数、报价、工期、有效期、保证金、资格、承诺、完整性与模板覆盖。",
    system_prompt="""工作目标：
1. read_facts；若没有事实，提示先运行招标解析 Agent。
2. 调用 audit_document 执行逐项核对。
3. 对阻断级、严重级、主要级问题逐条整理；不要重复合并不同章节的问题。
4. finish，返回 issues、stats 与一句总体结论。
只报告能从成稿和招标事实中核实的问题；签字、盖章、封装、原件等放入人工核对。""",
    tools=[TOOLS["read_facts"], TOOLS["list_sections"], TOOLS["audit_document"]],
    max_steps=4,
    assistant_key="reviewer",
)


FIXER = AgentSpec(
    key="fixer",
    name="修复 Agent",
    description="针对核对问题定位章节、改写正文，并在修改后重新核对。",
    system_prompt="""工作目标：
1. read_facts 与 list_sections。
2. 输入参数 issues 是待修复问题列表；按严重程度优先处理，最多修改 3 个章节。
3. 对每个问题调用 fix_section，传入 sectionId、finding、recommendation。
4. 修改完成后调用 audit_document 复查。
5. finish，返回 modified 章节与 remaining issues 数量。
不得改写与问题无关的内容，不得引入新的资质、业绩或参数。""",
    tools=[
        TOOLS["read_facts"],
        TOOLS["list_sections"],
        TOOLS["read_section"],
        TOOLS["fix_section"],
        TOOLS["audit_document"],
    ],
    max_steps=8,
    assistant_key="reviser",
)


TEMPLATE_AGENT = AgentSpec(
    key="template",
    name="模板 Agent",
    description="识别并抽取 DOCX/PDF 模板章节，核对成稿对模板的覆盖情况，为三种生成模式提供模板依据。",
    system_prompt="""工作目标：
1. detect_mode 判断模板来源。
2. extract_template 抽取模板章节。
3. list_sections 查看成稿章节，核对模板覆盖。
4. finish，返回 mode、template chapters、missing chapters 与装配建议。
只依据解析结果，不得虚构模板章节。""",
    tools=[TOOLS["detect_mode"], TOOLS["extract_template"], TOOLS["list_sections"]],
    max_steps=4,
    assistant_key="analyst",
)


AGENTS: dict[str, AgentSpec] = {
    agent.key: agent
    for agent in (TENDER_READER, PLANNER, BUILDER, AUDITOR, FIXER, TEMPLATE_AGENT)
}
