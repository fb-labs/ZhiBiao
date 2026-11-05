from __future__ import annotations

PLAN_SYSTEM = """你是本项目投标文件的正文总编，只负责编排，不写正文。
结合完整目录、上下级与同级关系、已确认的项目事实，为每一节确定唯一写作范围。
每节用 1 至 2 句话说明写什么、从哪个角度写、与相邻章节如何分工。
不得编造参数、人员、设备、品牌、型号、周期、业绩、证书、价格或承诺。
有下级章节的分支只说明总体范围和下级分工，不重复子章节的具体措施。
相同事项只分配给一个最合适章节。只输出严格 JSON：
{"plans":[{"seq":1,"writing_focus":"..."}]}"""


def planner_messages(sections: list[dict], facts: str) -> list[dict]:
    lines = []
    for index, section in enumerate(sections, start=1):
        depth = 1 if not section.get("parentKey") else 2
        indent = "  " * (depth - 1)
        lines.append(f"{indent}seq={index} {section.get('heading')}")
    directory = "\n".join(lines)[:12000]
    return [
        {"role": "system", "content": PLAN_SYSTEM},
        {
            "role": "user",
            "content": f"【项目事实】\n{facts[:6000] or '（暂无已确认事实）'}\n\n【完整目录】\n{directory}\n\n请为每个 seq 输出 writing_focus。",
        },
    ]


WRITE_SYSTEM = """你是投标文件正文撰写专家，文字严谨、务实、可核查。
只使用用户提供的项目事实与招标信息，不得编造资质、证书编号、业绩、人员姓名、品牌型号、价格或未提供的承诺。
对评分项逐条响应，给出可执行的做法、量化指标与交付物；缺失信息保留“（待补充）”占位，不要用空话掩盖。
不得出现“详见招标文件”“以招标文件为准”“我方无法”“仅供参考”等推诿表述。
不要重复其他章节的内容，不要输出章节标题本身，不要解释写作过程。
正文使用 Markdown：可用 ## 小标题、列表、表格；确实需要示意图时，单独一行写 `{{图片占位:图名与说明}}`（不要写成扫描件或真实证照要求）。"""


def chapter_messages(
    tender_title: str,
    heading: str,
    focus: str,
    scope: str,
    target_words: int,
    facts: str,
    scoring: str,
    siblings: str,
    context: str,
) -> list[dict]:
    user = f"""项目名称：{tender_title}
当前章节：{heading}
写作范围：{focus or '按招标文件要求完整响应本节内容'}
本章定位：{scope}
目标篇幅：约 {target_words} 字（允许上下浮动 20%）

【已确认的项目事实】
{facts or '（暂无已确认事实，缺失信息保留占位）'}

【本节匹配的评分项】
{scoring or '（暂无匹配评分项）'}

【同级章节（不要重复其内容）】
{siblings or '（无）'}

【已完成相邻章节摘要】
{context or '（无）'}

请直接输出本节正文，不要重复章节标题。"""
    return [
        {"role": "system", "content": WRITE_SYSTEM},
        {"role": "user", "content": user},
    ]


CONTINUE_SYSTEM = """你是投标文件正文撰写专家。用户会给出已写正文的末尾，请紧接其后继续写。
要求：从被中断的句子继续；保持原有语言风格、层级与编号；不重复已写内容；不重新输出标题；不写说明性文字。"""


def continue_messages(heading: str, tail: str, target_words: int, written: int) -> list[dict]:
    return [
        {"role": "system", "content": CONTINUE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"章节：{heading}\n目标篇幅约 {target_words} 字，当前已写约 {written} 字。\n"
                f"以下是已写正文末尾：\n{tail}\n\n请紧接这一段继续写，补足本节内容。"
            ),
        },
    ]
