"""Conservative, deterministic guards for generated bids; tender facts are not bidder evidence."""
from __future__ import annotations

import re
from typing import Any

_AUTHORED = ("技术性能", "技术方案", "技术描述", "技术支持", "施工方案", "实施方案",
             "技术措施", "售后服务", "服务计划", "服务方案", "培训方案", "组织方案",
             "进度计划", "质量保证方案", "安全方案", "应急方案", "供货方案", "包装运输方案",
             "信誉要求", "信用查询", "管理体系认证")
_AUTHORED_VARIANTS = ("技术响应", "技术建议", "技术规格说明", "解决方案", "施工组织", "组织设计",
                      "实施计划", "实施组织", "项目实施", "建设方案", "开发方案", "迁移方案",
                      "运维方案", "运维服务", "售后保障", "服务保障", "培训", "进度", "应急",
                      "质量保证", "质量保障", "安全保障", "安全措施", "供货组织", "设备说明",
                      "组织", "安全", "说明", "响应方案")
_FIXED_FORM_TITLE = re.compile(r"投标函|响应函|报价|偏差表|偏离表|身份证明|授权委托|授权书|承诺书|协议书|基本情况表")
_PH = re.compile(r"\{\{\s*待填[:：]([^}]+)\}\}")
_HISTORY = re.compile(r"业绩|近年完成|历史项目|同类项目")


def _source_sections(source: str, service: bool) -> str:
    pattern = re.compile(
        r"(?m)^[ \t　]*(?P<marker>第[一二三四五六七八九十百0-9]+[章节篇部分]+|"
        r"[一二三四五六七八九十]+[、．.]|\d+(?:\.\d+)*[、．. \t])"
        r"[ \t　]*(?P<name>[^\n]{1,80})[ \t　]*$")
    headings = list(pattern.finditer(source))
    has_major = any(h.group('marker').startswith('第') for h in headings)
    def depth(h):
        marker = h.group('marker')
        if marker.startswith('第'):return 1
        if re.match(r"[一二三四五六七八九十]", marker):return 2 if has_major else 1
        return len(re.findall(r"\d+", marker)) + (1 if has_major else 0)
    ranges = []
    for index, heading in enumerate(headings):
        name = heading.group('name').strip()
        if re.search(r"[.．·…]{2,}|\s+\d+\s*$", name):
            continue
        relevant = bool(re.search(r"供货要求|技术规格|技术规范|技术要求|采购.*需求|项目需求|"
                                  r"服务需求|服务内容|服务要求|工作任务|施工要求|建设内容|功能需求", name))
        if service and re.fullmatch(r"合同条款(?:及格式)?|合同条件", name):relevant = True
        if not relevant:
            continue
        end = next((h.start() for h in headings[index+1:] if depth(h)<=depth(heading)),len(source))
        if ranges and heading.start()<=ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(end,ranges[-1][1]))
        else:ranges.append((heading.start(),end))
    return "\n\n".join(source[start:end] for start,end in ranges) or source


def requires_authored_content(title: str, content: str = "", source: str = "") -> bool:
    value = re.sub(r"\s+", "", str(title or ""))
    if _FIXED_FORM_TITLE.search(value):
        return False
    if any(hint in value for hint in (*_AUTHORED, *_AUTHORED_VARIANTS)):
        return True
    if str(source or '').startswith(('大模型', '内置模板', '投标检查修复')):
        return True
    return bool(content and re.search(r"自拟|自定|自行编|自行填写",content)
                and is_instruction_only(content,title))


def is_manual_content(source: str) -> bool:
    # Model diagnostics such as “需人工补齐” do not mean the user edited it.
    return str(source or "").strip().startswith("人工编辑")


def is_instruction_only(content: str, title: str) -> bool:
    """A title/instruction page is not a completed chapter (do not measure by length alone)."""
    lines = []
    clean_title = re.sub(r"[\s#、.．一二三四五六七八九十\d（）()]+", "", title or "")
    for raw in str(content or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        normalized = re.sub(r"[\s#、.．一二三四五六七八九十\d（）()]+", "", line)
        if normalized == clean_title:
            continue
        instruction = re.sub(r"^(?:#+\s*|[（(]?\d+[、.）)]\s*|(?:注|说明|编制说明|填写要求)[:：]\s*)+", "", line)
        if re.fullmatch(r"(?:本章|本节|本部分|本表|本方案)?(?:内容)?(?:应|须|可)?(?:由)?(?:投标人|供应商|响应人)"
                        r"(?:应|须|需|可)?(?:根据[^。\n]{0,100}|结合[^。\n]{0,100}|自行|自拟|自定|按[^。\n]{0,100})"
                        r"(?:编写|编制|拟定|填写|补充|提供|描述)[^。\n]{0,40}[。.]?", instruction):
            # Actual obligations containing measurable values are not just instructions.
            if not re.search(r"\d\s*(?:台|套|份|米|天|日|小时|人|mm|kW|%|％)", instruction):
                continue
        if re.fullmatch(r"(?:本章|本节|本部分)?(?:格式|内容|方案)?(?:由投标人)?(?:自行编制|自行编写|自拟|自定)[。.]?", instruction):
            continue
        if re.fullmatch(r"(?:注[:：]?\s*)?(?:投标人应|请投标人|本节由投标人)"
                        r".*(?:根据|结合|自行|自拟).*(?:描述|填写|编制|提供|补充)[。.]?", line):
            continue
        if line in ("格式自拟", "注：格式自拟。", "注：", "注:"):
            continue
        lines.append(line)
    return not lines


def repair_generated_text(text: str, title: str = "", *, project_name: str = "",
                          manual: bool = False) -> tuple[str, list[str]]:
    """Only retract claims contradicted by missing evidence in the generated draft.

    Manual text is the owner's assertion and is never rewritten by this guard. An
    absent bidder certificate/contract must never be inferred from a tender name.
    """
    if manual:
        return str(text or ""), []
    original = str(text or "")
    notes: list[str] = []
    from zhibiao.core.tail_repair import NOTE_RE
    cleaned_lines = []
    for line in original.splitlines():
        if NOTE_RE.match(line):
            notes.append("该章节续写未完成，须人工补齐并核对")
        else:
            cleaned_lines.append(line)
    original = "\n".join(cleaned_lines)
    history = bool(_HISTORY.search(title))
    unresolved = bool(_PH.search(original))
    lines = original.splitlines()
    header: list[str] = []
    row_no = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if any(c in ("项目名称", "发票查验状态", "查验状态") for c in cells):
                header = cells
                row_no = 0
                continue
            if not header or all(re.fullmatch(r"[:\-\s]*", c) for c in cells):
                continue
            if len(cells) != len(header):
                continue  # Never guess a broken table's column alignment.
            row_no += 1
            missing = any(_PH.search(c) for c in cells) or any(
                not c and re.search(r"发票号码|发票开具|合同签订|买方名称|合同价格|项目名称", label)
                for c, label in zip(cells, header))
            for col, label in enumerate(header):
                if history and label == "项目名称" and project_name and cells[col] == project_name:
                    cells[col] = "{{待填:业绩%d项目名称}}" % row_no
                    notes.append("历史业绩不得预填本次项目")
                if missing and "查验状态" in label and re.search(r"已查验|查验通过|已通过", cells[col]):
                    cells[col] = "{{待填:业绩%d发票实际查验结果}}" % row_no
                    notes.append("缺少发票信息时撤回查验结论")
            lines[index] = "| " + " | ".join(cells) + " |"
        else:
            header = []
        # Correct a field's meaning, not the bidder's (unknown) actual amounts.
        fixed = re.sub(r"(税额(?:为|是|[:：]))\s*\{\{待填:投标总报价(?:小写|金额)?\}\}",
                       r"\1{{待填:税额}}", lines[index])
        fixed = re.sub(r"((?:不含税价|不含税报价)(?:为|是|[:：]))\s*\{\{待填:投标总报价(?:小写|金额)?\}\}",
                       r"\1{{待填:不含税报价}}", fixed)
        if fixed != lines[index]:
            notes.append("价税字段分离")
        lines[index] = fixed
    out = "\n".join(lines)
    # The tender specifies both periods but does not authorize this exclusion.
    out, count = re.subn(
        r"与(\d+)\s*(日历天|天|日)服务期分别计时，不相互压缩",
        r"与\1\2服务期的衔接须经招标人书面确认（{{待填:相关期限衔接确认}}）", out)
    if count:
        notes.append("撤回未经确认的期限排除解释")
    if unresolved:
        # Restrict this to the same sentence; an unrelated missing ISO14001 field
        # must not invalidate a separately supplied ISO9001 statement.
        cert_pattern = re.compile(r"(?:我方|投标人)(?:已经|已)(?:通过|取得|获得)"
                                  r"([^。；\n]{0,65}?(?:认证|证书))([^。\n]*)")
        def certificate_claim(match: re.Match) -> str:
            if not re.search(r"待填:[^}]{0,40}(?:证书|认证)", match.group(0)):
                return match.group(0)
            notes.append("证书待填时撤回已认证断言")
            return ("我方" + match.group(1) + "情况以真实有效的证明材料为准"
                    "（{{待填:相应认证或证书实际取得情况}}）" + match.group(2))
        out = cert_pattern.sub(certificate_claim, out)
        out, count = re.subn(
            r"(?:我方提交的)?审计报告复印件已完整覆盖([^。\n]+)",
            r"拟提交的审计报告应覆盖\1；实际材料提交情况为{{待填:审计报告实际提交情况}}", out)
        if count:
            notes.append("审计材料改为待实际提交")
        if history:
            replacements = {
                "我方上述业绩在下列维度与本项目高度可比": "历史业绩与本项目的可比性应据真实合同逐项核验（{{待填:业绩可比性核验结论}}）",
                "与我方历史供货产品的参数区间一致": "与历史供货产品的比较结论为{{待填:历史产品参数比较结论}}",
                "与我方既有产品配置完全对应": "与既有产品的配置比较结论为{{待填:历史产品配置比较结论}}",
                "各业绩项目均由我方作为整机制造商直接供货": "各业绩项目的真实供货主体为{{待填:各项历史业绩供货主体}}",
            }
            for before, after in replacements.items():
                if before in out:
                    out = out.replace(before, after)
                    notes.append("历史业绩比较改为待核验")
            out, count = re.subn(
                r"(?:本节所列全部业绩(?:项目)?|表中每一项业绩)均为我方[^。\n]+[。]?",
                "本节仅申报具有真实合同、发票等证明材料的历史业绩；实际完成及交付情况为{{待填:各项业绩真实履约情况}}。", out)
            if count:
                notes.append("撤回待填业绩的既成事实断言")
            out, count = re.subn(
                r"上表所列项目均满足[:：][^。\n]+[。]?",
                "上表业绩的时间、地点、类型及证明材料须逐项核验，核验结论为{{待填:各项历史业绩核验结论}}。", out)
            if count:
                notes.append("待填业绩不得预先判满足")
            out, count = re.subn(
                r"每项发票均已通过国家税务总局全国增值税发票查验平台完成查验并留存截图\s*\|\s*符合",
                "{{待填:逐项真实发票查验结果及截图}} | 待核验", out)
            if count:
                notes.append("撤回未提供发票的查验及符合结论")
            for pattern, replacement in (
                (r"本节所列业绩对应的发票均已开具并交付业主单位[^。\n]*[。]?",
                 "本节各项业绩的实际开票、交付及合同对应情况为{{待填:业绩发票与合同实际核验情况}}。"),
                (r"本节所列业绩中涉及的设备均已按合同约定完成交付[^。\n]*[。]?",
                 "本节各项业绩的实际交付、验收及运行情况为{{待填:业绩交付验收与运行证明}}。"),
                (r"本节所列业绩的合同金额、签订时间、业主单位、设备规格型号等信息与合同原件完全一致[^。\n]*[。]?",
                 "本节业绩字段须与真实合同原件逐项核对，实际核对结论为{{待填:业绩字段与合同原件核对结论}}。"),
            ):
                out, count = re.subn(pattern, replacement, out)
                if count:
                    notes.append("撤回待填业绩的既成事实断言")
        if "技术支持" in title:
            # A report number still to be supplied is not a submitted attachment.
            for before, after in (
                ("我方随投标文件提交以下", "拟随投标文件提交以下"),
                ("我方提交由具备相应资质的检测机构出具的检测报告", "拟提交由具备相应资质的检测机构出具的真实检测报告"),
                ("针对本项目实质性技术要求提供完整的技术支持资料", "拟针对本项目实质性技术要求准备技术支持资料，实际提交及核验情况为{{待填:技术支持资料实际提交与核验情况}}"),
            ):
                if before in out:
                    out = out.replace(before, after)
                    notes.append("技术资料提交状态改为待核验")
        if re.search(r"税收|社会保障|社保", title):
            for before, after in (
                ("不存在欠缴、少缴税款情形", "实际有无欠缴、少缴税款以真实纳税记录核验（{{待填:实际纳税状态}}）"),
                ("不存在欠缴社会保险费情形", "实际有无欠缴以真实社保缴费记录核验（{{待填:实际社保缴费状态}}）"),
                ("所载社会保险缴费记录连续、正常，无欠缴情形", "实际连续性及有无欠缴为{{待填:社保记录连续性及欠缴核验结论}}"),
            ):
                if before in out:
                    out = out.replace(before, after)
                    notes.append("纳税社保状态改为据实核验")
        if re.search(r"诉讼|仲裁", title):
            out, count = re.subn(
                r"经内部法务部门对近三年全部涉诉、涉裁档案进行核查，并同步在([^。\n]+?)完成检索比对",
                r"拟由投标人核查近三年涉诉、涉裁档案，并在\1据实检索比对（{{待填:实际法务核查及查询结果}}）", out)
            if count:
                notes.append("法务查验完成状态改为待核验")
            out = out.replace("所有案件均已按本节8.5.3表格逐案列明，并附相应法律文书复印件作为证明材料。",
                              "实际案件须逐案列明并附真实法律文书，{{待填:案件清单及法律文书实际提交情况}}。")
        out, count = re.subn(
            r"本表所列(\d+)项已覆盖([^。\n]+?)的全部条目，无任何遗漏",
            r"本表所列\1项须与\2逐条核对，完整性核验结论为{{待填:技术要求逐项覆盖核验结论}}", out)
        if count:
            notes.append("撤回未经逐项核对的完整覆盖结论")
        if re.search(r"财务|履约能力", title):
            out, count = re.subn(
                r"(?:我方|投标人)(?:财务状况稳健，资金链充足|具备[^。\n]{0,80}?财务实力与持续经营能力，近三年财务状况稳定)[^。\n]*[。]?",
                "财务履约能力以实际审计报告及资金安排为准，{{待填:实际财务状况及履约资金保障依据}}。", out)
            if count:
                notes.append("财务待填时撤回稳健充足断言")
    # A missing item may be in a table after the assertion, rather than in the
    # same sentence. Limit the retraction to the same explicitly named system.
    for system in ("ISO9001", "ISO14001", "ISO45001"):
        system_re = re.compile(r"(?<![A-Za-z0-9])" + system[:3] + r"\s*" + system[3:] + r"(?![A-Za-z0-9])", re.I)
        lines = out.splitlines()
        missing_system = any(system_re.search(line) and _PH.search(line) for line in lines)
        if not missing_system:
            continue
        for i, line in enumerate(lines):
            if not system_re.search(line) or _PH.search(line) or "|" in line:
                continue
            if re.search(r"(?:我方|本公司|本企业|投标人)[^。；\n]{0,30}(?:已建立|已经建立|已通过|已取得|已获得|有效运行)", line):
                lines[i] = re.sub(r"[^。；\n]*(?:我方|本公司|本企业|投标人)[^。；\n]*" + system_re.pattern + r"[^。；\n]*[。；]?",
                                  "相关管理体系的实际建立、认证和运行情况以真实材料为准（{{待填:管理体系实际情况及证明}}）。", line, flags=re.I)
                notes.append("管理体系材料待填时撤回已建立或已认证断言")
        out = "\n".join(lines)
    if re.search(r"财务|履约能力|资金", title) and any(
        re.search(r"资产|负债|审计|财务|授信|资金", ph.group(1)) for ph in _PH.finditer(out)
    ):
        out, count = re.subn(
            r"[^。；\n]*(?:近三年|我方|投标人)[^。；\n]*(?:流动资产|资金链|授信额度|逾期付款)[^。；\n]*[。；]?",
            "相关财务状况及资金保障能力以真实审计报告、付款记录和授信材料核验（{{待填:财务状况及资金保障依据}}）。", out)
        if count:
            notes.append("财务证据待填时撤回资金及授信断言")
    # Statement-local evidence guard: alternate wording and certificate types
    # receive the same treatment; unrelated missing fields do not retract proof.
    def evidence_statement(match):
        statement=match.group(0)
        missing=any(re.search(r"证书|认证|资质|报告|查验|核验|合同|业绩|发票",ph.group(1))
                    for ph in _PH.finditer(statement))
        missing=missing or bool(re.search(r"(?:证书|报告|查验结果|核验结果)[^。；\n]{0,12}(?:未提供|未上传|待补)",statement))
        if not missing:return statement
        if not re.search(r"证书|认证|资质|报告|查验|核验|合同|业绩|发票",statement):return statement
        fixed,count=re.subn(r"(我方|本公司|本企业|投标人)(?:已经|已)(通过|取得|获得|具备|完成|提交|提供|附具)",
                            r"\1拟据实说明\2情况（尚待真实材料核验）：",statement)
        if count:notes.append("缺少对应证明时撤回已具备或已完成断言")
        return fixed
    out=re.sub(r"[^。；\n]+[。；]?",evidence_statement,out)
    return out, list(dict.fromkeys(notes))


def source_backed_section(title: str, source: str) -> str:
    """Honest fallback for legacy title-only sections; no model or invented product data.

    Source excerpts retain their scope and are labelled as requirements. Proposed
    measures and missing supplier evidence are explicitly distinct.
    """
    from zhibiao.core.requirements import build_requirement_inventory

    if re.search(r"信誉要求|信用查询|管理体系认证", title):
        if "管理体系认证" in title:
            labels = ["本企业管理体系认证证书及真实查询结果"]
        else:
            from zhibiao.core.credit_proofs import credit_channels_in_source
            labels = credit_channels_in_source(source) or ["招标要求的信用查询渠道"]
        blocks = ["按招标要求查询投标人本企业的实际状态。招标文件中的参考截图不作为本企业证明；查询结果及材料由实际核验后填写。"]
        for label in labels:
            blocks += ["### " + label, "查询对象：{{待填:投标人企业全称}}；实际结果：{{待填:%s实际查询结果}}。" % label,
                       "{{图片占位:投标人本企业%s真实查询截图}}" % label]
        return "\n\n".join(blocks)

    service = bool(re.search(r"服务|质保|培训", title))
    scoped_source = _source_sections(source, service)
    inventory = build_requirement_inventory(scoped_source)
    if "技术支持" in title:
        categories = {"evidence", "deliverables"}
    elif re.search(r"服务|质保|培训", title):
        categories = {"warranty_sla", "training", "acceptance", "schedule", "deliverables"}
    else:
        categories = {"quantities_models", "deliverables"}
    records = []
    used = 0
    for record in inventory.get("requirements", []):
        if not categories.intersection(record.get("categories", [])):
            continue
        raw = str(record.get("raw") or "").strip()
        # A multi-line inventory record can join a blank price-total row to
        # a genuine service-cost clause. Keep the clause, discard the empty row.
        raw = "\n".join(line for line in raw.splitlines() if not (
            re.match(r"\s*(?:合计|总计)", line) and
            re.search(r"人民币\s*[【\[]\s*[】\]]", line))).strip()
        path = " > ".join(record.get("source", {}).get("section_path") or [])
        # Avoid copying the sample forms or evaluation procedure as product requirements.
        if re.search(r"投标文件格式|投标文件有关格式|评标办法|评审办法", path):
            continue
        # Inventory categories may be inherited from a whole chapter. Select
        # evidence/service clauses by their actual text, not inherited tags.
        if "技术支持" in title and not re.search(
                r"资料|文档|源代码|图纸|图册|说明书|手册|证书|合格证|标志|认证|报告|证明|检验|检测|监控.*点", raw):
            continue
        if service and not re.search(
                r"质保|保修|售后|维修|维护|巡检|运维|工单|故障|培训|调试|验收|安装|指导|响应|服务|交货|供货期|交付|备件", raw):
            continue
        if not raw or used + len(raw) > 12000:
            continue
        records.append({**record, "raw": raw})
        used += len(raw)
    parts = ["## 招标要求与拟响应", "以下要求摘自本项目招标原文；实际响应、交付内容、证明材料和人员安排须由投标人确认，招标要求不作为已完成检验或已具备资质的证明。"]
    if records:
        for number, record in enumerate(records, 1):
            loc = record.get("source") or {}
            label = str(loc.get("section") or "招标原文")
            parts += ["### 要求%d %s" % (number, label),
                      "招标要求摘录：\n" + str(record["raw"]).strip()]
        parts.append("拟响应：按上述要求的范围、对象、数值、单位和起算条件编制供货及履约方案，不将原文参数作为已完成检验的结果。逐项实际响应及对应证据为{{待填:逐项实际响应与证明材料索引}}。")
    else:
        if (not service and "技术支持" not in title and len(scoped_source)<=1200
                and not re.search(r"投标文件格式|评标办法|投标人须知",scoped_source)
                and re.search(r"\d\s*(?:mm|kW|kN|V|MPa|小时|天|日|年|台|套)", scoped_source)):
            # A short parameter-only section may be classified as a heading by
            # the inventory parser. Retain its exact scoped text instead of
            # turning already supplied values back into empty placeholders.
            parts.append("招标要求摘录：\n" + scoped_source.strip())
        else:
            parts.append("{{待填:本节对应的招标要求原文及逐项响应}}")
    if "技术支持" in title:
        parts += ["## 真实技术资料清单", "按适用要求提供制造商技术文件、对应型号证书、检验报告、图纸及使用维护资料；资料名称、型号、版本、出具单位、有效期及所在页码逐项列明。",
                  "| 材料 | 实际编号及型号 | 提交情况 |", "| --- | --- | --- |",
                  "| 技术文件及图纸 | {{待填:实际资料名称型号版本}} | {{待填:实际附件及页码}} |",
                  "| 适用证书和检验报告 | {{待填:真实有效证书及报告编号}} | {{待填:实际附件及页码}} |"]
    elif re.search(r"服务|质保|培训", title):
        parts += ["## 拟实施流程", "按原文约定安排启动交接、工作实施、过程记录、成果交付、培训及验收配合。接到服务请求后登记事项、按原文时限响应并安排处理，记录处理结果并回访；费用承担、保障范围及起算条件分别按对应条款执行。",
                  "服务人员及联系方式：{{待填:投标人服务人员及联系方式}}。履约资源与保障安排：{{待填:投标人实际履约资源与服务安排}}。"]
    else:
        parts += ["## 实际响应确认", "按各采购对象分别填写实际配置、响应参数、实施措施及证据位置；参考内容不自动改为唯一实现方式，单项配置和项目总量分别核对。",
                  "实际技术配置、实施方案及响应依据：{{待填:实际响应内容和证明材料}}。"]
    return "\n\n".join(parts)
