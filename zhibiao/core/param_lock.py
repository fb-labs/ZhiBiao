"""Source-backed parameter lock and device registry.

Device tables retain headings, source order, repeated rows and assembly/column
ownership. Procurement aliases merge into one object; all values are extracted
from this input, not project constants. Background capability claims remain
context and never replace formal table values. Supplemental material/hydraulic/
commercial excerpts retain the existing build_param_lock interface.
"""

from __future__ import annotations

import re

# ── 桶一：力学与尺寸参数表 ──
_TABLE_KEYS = (
    "初撑力", "工作阻力", "支护高度", "支架高度", "支架宽度", "支架长度", "支架重量",
    "立柱数量", "支架数量", "缸径", "柱径", "行程", "倾角", "推移", "侧推", "护帮",
    "伸缩", "外形尺寸", "不可拆卸件", "吨/组", "根/组",
)
_MATERIAL_KEYS = ("Q690", "Q550", "Q460", "Q345", "27SiMn", "30CrMnTi", "σb", "σs",
                  "抗拉强度", "屈服强度", "钢板", "材质")
# ── 桶二：液压系统与管径 ──
_HYDRAULIC_KEYS = ("DN10", "DN12", "DN16", "L/min", "安全阀", "液控单向阀", "换向阀",
                   "压力传感器", "反冲洗", "自锁球型", "截止阀", "断路阀", "胶管",
                   "公称直径", "保护套", "立装", "乳化液", "泵站", "MPa", "Mpa", "kN")
# ── 桶三：标准、包装运输贮存、检验与售后 ──
_STD_KEYS = ("GB/T", "GB ", "MT/T", "NB/T", "ISO", "煤安", "贮存", "运输", "包装",
             "中检", "转动活塞杆", "工作日", "小时内", "分钟内", "响应", "质保", "售后",
             "相对湿度", "℃", "环境")
_BUCKETS = (
    ("力学与尺寸参数", _TABLE_KEYS + _MATERIAL_KEYS, 4200),
    ("液压系统与管径", _HYDRAULIC_KEYS, 2600),
    ("标准、包装贮存、检验与售后", _STD_KEYS, 2600),
)
_DN_CLAUSE_RE = re.compile(r"立柱下腔.{0,80}?DN\s*(\d+).{0,80}?立柱上腔.{0,60}?DN\s*(\d+)")
_HOSE_MODEL_RE = re.compile(r"DN(\d+)-\d+-\d+")
_STD_RE = re.compile(r"(?:GB/T|MT/T|NB/T|GB)\s*\d{3,5}(?:\.\d+)?\s*[-—]\s*\d{4}")




# Names are terminology aliases, never a registry of project-specific values.
_DEVICE_ALIASES = {
    "端头联排液压支架": ("端头联排液压支架", "端头联排支架"),
    "超前联排液压支架": ("超前联排液压支架", "超前联排支架"),
    "中置式大阻力端尾支架": ("中置式大阻力端尾支架", "端尾支架"),
}
_FIELD_RE = re.compile(
    r"(?P<name>最大不可拆卸件重量|最大运输外形尺寸|整组支架重量|支架工作阻力|"
    r"支架数量|支架高度|支架宽度|支架长度|支架重量|初撑力|工作阻力|"
    r"平均支护强度|底板平均比压|支护强度|对底板比压|泵站压力|移架步距|"
    r"伸缩梁行程|侧推油缸最大行程|缸径/柱径|缸径/杆径|缸径|柱径|推力/拉力|推溜力/移架力|数量|行程)"
    r"\s+(?P<value>[≤≥～~约]?\s*[Φφ]?\d[\d.Φφ/×*±%～~—–\-]*?)"
    r"\s+(?P<unit>kN|MPa|Mpa|mm|m|吨|t|Kg|kg|根|组|架)"
    r"(?P<per>\s*[/\\]\s*(?:组|架))?(?=\s|$)", re.I)
_TABLE_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.、]?\s*)?"
    r"(?P<name>[^\n:：]{2,45}?支架)(?P<kind>基本参数|立柱与千斤顶技术要求)"
    r"(?:[（(]单组[）)])?\s*$")


def _canonical_device(name: str) -> str:
    name = name.strip()
    for canonical, aliases in _DEVICE_ALIASES.items():
        if name in aliases:
            return canonical
    return name


def extract_device_registry(text: str) -> list[dict]:
    """Extract source-backed device objects, without inventing missing values.

    Each object has canonical_name, aliases, quantity, parameters, tables and context.
    Parameters preserve name/value/unit/raw/source and level (assembly, column or
    component). source contains 1-based line_start/line_end/heading. Pressure is
    supplementary, never multiplied by column counts to override assembly rows.
    quantity preserves the procurement expression (e.g. '2架1组') as well as the
    assembly count and optional frames_per_group. Unknown headings remain distinct.
    Tables retain raw headers and row order including PDF continuation lines.
    Narrative claims remain context; they cannot override a basic-parameter row.
    """
    raw = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    lines = raw.splitlines()
    devices: dict[str, dict] = {}

    def ensure(name: str) -> dict:
        canonical = _canonical_device(name)
        if canonical not in devices:
            aliases = list(_DEVICE_ALIASES.get(canonical, (canonical,)))
            devices[canonical] = dict(canonical_name=canonical, aliases=aliases,
                                     quantity=None, parameters=[], tables=[], context=[])
        return devices[canonical]

    # Procurement quantities require adjacent parentheses, not a distant number.
    # Flatten only for this bounded expression so PDF soft wraps remain supported.
    for canonical, aliases in _DEVICE_ALIASES.items():
        for alias in aliases:
            m = re.search(re.escape(alias) + r'\s*[（(]\s*(\d+\s*架\s*)?(\d+)\s*组\s*[）)]', raw)
            if m:
                device = ensure(canonical)
                quantity = dict(value=m.group(2), unit='组', raw=m.group(0),
                                source=dict(line_start=raw.count('\n', 0, m.start())+1,
                                            line_end=raw.count('\n', 0, m.end())+1,
                                            heading='采购数量'))
                if m.group(1):
                    quantity['frames_per_group'] = re.search(r'\d+', m.group(1)).group()
                device['quantity'] = quantity
                break

    starts = []
    for i, line in enumerate(lines):
        m = _TABLE_HEADING_RE.match(line)
        if m and m.group("name").strip() != "支架" and not re.match(r"^[（(]", m.group("name")):
            starts.append((i, m.group('name'), m.group('kind')))
    for start, name, kind in starts:
        device = ensure(name)
        level = 'assembly' if kind == '基本参数' else 'column'
        end = len(lines)
        for j in range(start+1, len(lines)):
            # The next table or numbered prose section terminates the current table;
            # page furniture and wrapped cells do not. Do not absorb later narrative.
            if (_TABLE_HEADING_RE.match(lines[j]) or
                    re.match(r'^\s*(?:[（(][一二三四五六七八九十]+[）)]|[一二三四五六七八九十]+、)', lines[j]) or
                    re.match(r'^\s*\d+[.、]\s*[^\d\s]', lines[j])):
                end = j
                break
        header_lines = []
        for line in lines[start+1:min(start+6,end)]:
            if '序' in line or '项目' in line or '号 ' in line:
                header_lines.append(line)
        source = dict(line_start=start+1, line_end=end, heading=lines[start].strip())
        table = dict(heading=lines[start].strip(), header='\n'.join(header_lines),
                     raw='\n'.join(lines[start:end]), source=source, level=level)
        device['tables'].append(table)
        current_level = level
        for i in range(start+1,end):
            line = lines[i]
            if level == 'column':
                # PDF tables split '2 推移千斤顶' into multiple lines. Only the
                # first numbered component is the main support column.
                if re.match(r'^\s*[2-9]\s*(?:$|推移|侧推|外侧|底调|伸缩|托梁|内侧)', line):
                    current_level = 'component'
            for m in _FIELD_RE.finditer(line):
                field = dict(name=m.group('name'), value=re.sub(r'\s+', '', m.group('value')),
                             unit=m.group('unit') + re.sub(r'\s+', '', m.group('per') or ''),
                             raw=line, level=current_level,
                             source=dict(line_start=i+1, line_end=i+1, heading=source['heading']))
                pressure = re.search(r'P\s*=\s*([\d.]+)\s*MPa', line, re.I)
                if pressure:
                    field['pressure'] = dict(value=pressure.group(1), unit='MPa')
                device['parameters'].append(field)
                if field['name'] == '支架数量' and not device['quantity']:
                    device['quantity'] = {k:field[k] for k in ('value','unit','raw','source')}

    # Preserve attributed background claims separately, rather than turning
    # '可达' into a conflicting mandatory table value or a globally banned number.
    table_lines = {i for d in devices.values() for t in d['tables']
                   for i in range(t['source']['line_start']-1,t['source']['line_end'])}
    for i,line in enumerate(lines):
        if i in table_lines or not re.search(r'可达\s*\d+\s*kN', line, re.I):
            continue
        block = '\n'.join(lines[max(0,i-2):min(len(lines),i+2)])
        owners = [d for d in devices.values() if any(a in block for a in d['aliases'])]
        if len(owners) == 1:
            owners[0]['context'].append(dict(raw=block, level='context',
                source=dict(line_start=max(0,i-2)+1,line_end=min(len(lines),i+2),heading='背景能力描述')))
    return list(devices.values())


def _registry_lock(registry: list[dict]) -> str:
    if not registry:
        return ''
    parts = ['## 按设备分组的权威参数（表头、行序及归属保留）',
             '权威顺序：正式技术参数表 > 背景能力介绍。参数必须同时匹配设备对象、表格层级和单位；整架/单组表与单柱/部件表不得互相替代，'
             '不得用单柱力×柱数擅自改写整架原文。原文不明的整架/整组口径须保留原文并待澄清。'
             '背景“可达”能力仅作背景，不覆盖基本参数表；也不能因与表值不同而全局禁用该数字。']
    parts.append('本原文可确认的采购对象共%d类：%s；上述别名仅是同一对象的不同称谓，不另增采购项。' % (len(registry), '、'.join(d['canonical_name'] for d in registry)))
    for device in registry:
        parts.append('### ' + device['canonical_name'] + '（同一设备别名：' + '、'.join(device['aliases']) + '）')
        if device['quantity']:
            parts.append('采购数量原文：' + device['quantity']['raw'])
        for table in device['tables']:
            parts.append('[层级=%s；原文行%d-%d]\n%s' % (table['level'],
                table['source']['line_start'],table['source']['line_end'],table['raw']))
        for context in device['context']:
            parts.append('[背景能力，非基本表替代值]\n' + context['raw'])
    return '\n\n'.join(parts)


def _derived_rules(text: str) -> list[str]:
    """按原文生成明确口径（原文没写就不生成，不做假设）。

    PDF/Word 抽出的文本常把一句话折成多行（实测「立柱下腔…DN12，立柱上腔…DN10」
    正好被折行），所以先压平所有空白再匹配，否则最关键的管径口径规则会抽不出来。
    """
    rules: list[str] = []
    flat = re.sub(r"\s+", "", str(text or ""))
    m = _DN_CLAUSE_RE.search(flat)
    if m:
        low, high = m.group(1), m.group(2)
        rule = ("* 管径（原文字面口径）：立柱下腔和推移千斤顶上下腔胶管公称直径 DN%s；"
                "立柱上腔和其他千斤顶上下腔胶管公称直径 DN%s。" % (low, high))
        models = sorted({mm.group(0) for mm in _HOSE_MODEL_RE.finditer(flat)})
        if models:
            rule += "（%s 是胶管型号，不是公称直径，不得据此把上腔写成 DN%s）" % (
                "、".join(models[:3]), models[0].split("-")[0][2:])
        rules.append(rule)
    stds: list[str] = []
    for std in _STD_RE.findall(flat):
        clean = re.sub(r"\s+", "", std).replace("—", "-")
        if clean not in stds:
            stds.append(clean)
    if stds:
        old_warning = "，尤其不得写 GB/T 25974.1-2010" if "GB/T25974.1-2023" in stds else ""
        rules.append("* 标准号（对原文列明标准采用所列版本，不得擅自替换年份" + old_warning + "）："
                     + "、".join(stds) + "。")
    kinds: list[str] = []
    for label, aliases in _DEVICE_ALIASES.items():
        key = aliases[0]
        found = re.search(re.escape(key) + r".{0,24}?(\d+)(组|架)", flat)
        if found:
            kinds.append("%s %s %s" % (label, found.group(1), found.group(2)))
        elif key in flat:
            kinds.append(label)
    if kinds:
        rule = "* 结构型式（本项目的采购标的）：" + "、".join(kinds[:4]) + "。"
        if re.search(r"由原来.{0,30}单体液压支柱.{0,30}变更为", flat):
            rule += ("原文已明确「由原来多点单体液压支柱支护变更为…联排液压支架」，"
                     "因此严禁把端头/超前支架写成「单体液压支柱＋金属顶梁/铰接顶梁」——"
                     "那是本次招标要淘汰的旧工艺，写成单体支柱属于重大技术负偏离。")
        rules.append(rule)
    if text.strip():
        rules.append("* 术语纪律：「偏离项」只指对招标文件要求（参数、功能、工期等）的偏离；"
                     "制造过程中的质量缺陷、不合格品、返修不得称为「偏离项」。"
                     "确无偏离时只作无偏离声明，不得另行编写「重大偏离处置」内容。")
    return rules


def _pick(lines: list[str], keys: tuple[str, ...], budget: int, ctx: int = 10) -> list[str]:
    """桶内先保关键行（全部保留），再用上下文填满预算。"""
    hits = [i for i, line in enumerate(lines) if any(key in line for key in keys)]
    if not hits:
        return []
    primary = {i for i in hits}
    secondary: list[int] = []
    for i in hits:
        for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
            if j not in primary:
                secondary.append(j)
    out: list[str] = []
    used = 0
    selected = set(primary)
    used = sum(len(lines[i].strip()) + 1 for i in primary)
    for i in sorted(set(secondary)):
        size = len(lines[i].strip()) + 1
        if used + size <= budget:
            selected.add(i)
            used += size
    used = 0
    for i in sorted(selected):
        line = re.sub(r"[ \t]+", " ", lines[i].strip())
        if line:
            out.append(line)
            used += len(line) + 1
    return out


def render_device_parameters(registry: list[dict] | None, max_chars: int = 4000,
                             prefer: str = "") -> str:
    """把设备参数表渲染成可逐项照抄的权威清单，注入每章的共享上下文。

    为什么必须显式给出：正文提示词里原先只有「主参数清单」
    （source_fidelity.render_primary_contract，只解析"主要技术参数"这类小节），
    而真实标书的参数几乎都在表格里（"端头联排液压支架基本参数表""立柱参数表"），
    所以模型手里从来没有分层原值，只能靠上下文拼 —— 实测会把同一台设备的
    初撑力（10048kN P=25MPa）写成工作阻力（应为 12500kN P=34MPa），
    两轮修订仍在同一处出错，整章被判「正文质量未通过」而失败。

    level 用中文标注，避免模型把整架值与单柱值混用：
    assembly=整架、column=单柱（立柱）、component=部件（千斤顶/侧推等）。
    """
    _LEVEL_LABEL = {"assembly": "整架", "column": "单柱（立柱）", "component": "部件（千斤顶等）"}
    devices = [d for d in (registry or []) if isinstance(d, dict) and d.get("parameters")]
    if not devices:
        return ""
    hint = str(prefer or "")

    def _rank(device: dict) -> int:
        names = [str(device.get("canonical_name") or "")] + [str(a) for a in (device.get("aliases") or [])]
        return 0 if any(n and n in hint for n in names) else 1

    lines: list[str] = []
    used = 0
    truncated = False
    for device in sorted(devices, key=_rank):
        name = str(device.get("canonical_name") or "").strip()
        if not name:
            continue
        grouped: dict[str, list[dict]] = {}
        for param in device.get("parameters") or []:
            grouped.setdefault(str(param.get("level") or "assembly"), []).append(param)
        for level in ("assembly", "column", "component"):
            items = grouped.get(level) or []
            if not items:
                continue
            parts: list[str] = []
            for param in items:
                label = str(param.get("name") or "").strip()
                value = str(param.get("value") or "").strip()
                if not label or not value:
                    continue
                unit = str(param.get("unit") or "").strip()
                pressure = (param.get("pressure") or {}) if isinstance(param.get("pressure"), dict) else {}
                text = f"{label} {value}{unit}"
                if pressure.get("value"):
                    text += f"（P={pressure['value']}MPa）"
                parts.append(text)
            if not parts:
                continue
            line = f"{name}〔{_LEVEL_LABEL.get(level, level)}〕：" + "；".join(parts)
            if used + len(line) + 1 > max_chars:
                truncated = True
                break
            lines.append(line)
            used += len(line) + 1
        if truncated:
            break
    if not lines:
        return ""
    if truncated:
        lines.append("（参数表过长，其余设备以招标原文为准）")
    return "\n".join(lines)


def build_param_lock(text: str, *, max_chars: int = 14000) -> str:
    """返回最高优先级参数锁；max_chars仅限补充摘录，完整设备表/规则/商务时限不截断。"""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return ""
    lines = raw.split("\n")
    seen: set[str] = set()
    sections: list[str] = []
    for name, keys, budget in _BUCKETS:
        picked: list[str] = []
        for line in _pick(lines, keys, budget):
            if line in seen and not line.startswith("---"):
                continue
            seen.add(line)
            picked.append(line)
        if picked:
            sections.append("## %s\n%s" % (name, "\n".join(picked)))
    body = "\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars].rsplit("\n", 1)[0] + "\n…（原文摘录超长已截断，口径以上方规则为准）"

    header = ("【唯一参数口径 · 最高优先级】以下为招标文件原文摘录与口径规则，正文必须原样引用："
              "不得改写数值、单位、材质牌号、公称直径与标准号，不得用「按技术协议执行」"
              "「具体以合同为准」「详见投标文件」等空话替代具体参数。")
    rules = _derived_rules(raw)
    parts = [header]
    if rules:
        parts.append("## 口径规则（由招标文件原文生成）\n" + "\n".join(rules))
    structured = _registry_lock(extract_device_registry(raw))
    commercial = []
    for i, line in enumerate(lines):
        if re.search(r"服务期[：:]|质保期为|质保期满|本合同设备的质保期|合同签订后\s*\d+\s*日历天", line):
            commercial.append("[原文行%d] %s" % (i+1, "\n".join(lines[i:min(len(lines),i+2)])))
    if commercial:
        parts.append("## 商务时限（原文，独立保留不受摘录预算截断）\n" + "\n".join(dict.fromkeys(commercial)))
    if structured:
        parts.append(structured)
    if body:
        parts.append(body)
    return "\n\n".join(parts)
