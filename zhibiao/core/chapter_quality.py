"""Source-backed chapter checks. Never repair by swapping numbers globally."""
from __future__ import annotations
import re
from typing import Any

from zhibiao.core.chapter_rules import SUBHEADING_RE, heading_number
from zhibiao.core.image_slots import is_image_slot_line


def substantive_text(text: str) -> str:
    body = re.sub(r"<!--.*?-->", "", str(text or ""), flags=re.S)
    lines = [line for line in body.splitlines()
             if not re.match(r"^\s*(?:#{1,6}\s|```|~~~|[-*_]{3,}\s*$|\|?\s*:?-{3,})", line)]
    # 列表/任务框是排版标记，不是有效正文。限定行首且后接空白/行尾，
    # 不把 -20℃、-5~5MPa、AB-12 等真实负号/连字符当成列表删除。
    lines = [re.sub(r"^[ \t]*(?:(?:[-+*]|\d+[.)])(?:[ \t]+|$))+(?:\[[ xX]\](?:[ \t]+|$))?", "", line)
             for line in lines]
    return re.sub(r"[\s|*`#]+", "", "\n".join(lines))


def inline_numbering_issues(text: str, chapter_number: str) -> list[str]:
    """内嵌小标题必须落在本章节号体系内（见 chapter_rules.normalize_inline_numbering）。

    导出时章节标题按大纲统一定号，正文里的 ## 小标题若自带一套编号，Word 里就会出现
    与目录撞号、重复编号的标题（实测缺陷）。只认行首编号：标题文字里的「3 种工况」
    「ISO 9001」不是编号，不得误报；本章节号 + "." 开头（1.2、1.2.1、1.2.1.3）才算合法。
    """
    issues: list[str] = []
    number = str(chapter_number or "").strip()
    if not number:
        return issues
    for line in str(text or "").splitlines():
        matched = SUBHEADING_RE.match(line)
        if not matched:
            continue
        kind, value = heading_number(matched.group(2))
        if kind == "cjk":
            issues.append(f"内嵌小标题不得使用汉字序号，应使用 {number}.N")
        elif kind == "arabic" and not value.startswith(number + "."):
            issues.append("内嵌小标题编号与目录编号冲突")
    return issues


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("～", "-").replace("~", "-").replace("—", "-").replace("至", "-").replace(",", "").replace("，", "")


def _number(value: str) -> str:
    m = re.search(r"\d+(?:\.\d+)?(?:\s*[-~～—至]\s*\d+(?:\.\d+)?)?", str(value or ""))
    if not m:
        return ""
    return "-".join(str(float(x)).removesuffix(".0") for x in re.split(r"[-~～—至]", _norm(m.group())))


# 材质牌号：Q690、30CrMnTi、27SiMn、40Cr、ZG270-500…（用于「牌号必须在原文出现」的硬核对）
_GRADE = re.compile(r"(?:Q\d{3}[A-Z]?|\d{2}Cr(?:Mn|Ni|Mo)?[A-Za-z]{0,4}\d?|ZG\d{2,3}-?\d{3}|HT\d{3})")
# 真正的清单表头（序号 + 名称 + 数量/单位），用于区分「列了清单」与「提了一句清单」
_LIST_TABLE = re.compile(r"序号[^\n|]{0,6}\|[^\n]{0,40}?(?:名称|项目)[^\n]{0,60}?\|[^\n]*(?:数量|单位)")
# 真正的清单行：| 8 | 单向锁 | FDD200\50 | 件 | 6 |（有序号、单位、数量三列才对得上备件表）
_BILL_ROW = re.compile(
    r"(?m)^\s*\|\s*\d{1,3}\s*\|[^|\n]{1,30}\|[^|\n]{0,40}\|\s*(?:根/组|根|件|套|个|批|台|组)\s*\|\s*\d{1,3}\s*\|")
# 原文明确给了材质/涂装要求的标志；以及把这些要求写成空话的措辞
_EXPLICIT_SPEC = re.compile(r"底漆|面漆|喷漆|镀锌|钝化|镀铬|镗磨|棒材|阻燃|抗静电")
_VAGUE_SPEC = re.compile(r"按招标人要求(?:确定|选材|确定颜色)|按原文规定选材|颜色按招标人要求"
                         r"|由招标人确定(?:颜色|材质|涂装)|按招标文件要求确定(?:材质|颜色)")
# 「均/统一/一致」把多个设备的差异参数写成同一个值
_SHARED_CLAIM = re.compile(r"均(?:为|按|采用|取)|统一(?:为|按|采用|取)|一致|同为|都(?:为|按|采用)")
# 一句话里同时指到多台设备（用于判定"统一写"是否踩到设备差异）
_MULTI_DEVICE = re.compile(r"三类|三种|各型|上述|均|分别|联排|端头|超前|端尾")
# 破顶装置特有字段后紧跟的带单位数值（只核对这些）。
# 注意不能用裸的「行程/缸径」：那是每一根千斤顶都有的字段，
# 实测会把立柱行程 900、伸缩梁行程 500 误判成"破顶装置数值不在原文"。
_ROOF_VALUE = re.compile(
    r"(?:巷道高度|收回后高度|收回高度|伸出后高度|完全伸出后高度|破顶力)"
    r"[^。\n]{0,12}?(\d+(?:\.\d+)?(?:\s*[-~～—至]\s*\d+(?:\.\d+)?)?)\s*(m|mm|kN)"
    r"|破顶立柱[^。\n]{0,24}?(\d{2,3}/\d{2,3})\s*mm")

# 「这一节确实在写破顶装置本身」的标志：破顶后面跟着构造/参数词，而不是「破顶卸压」这种一句带过。
_ROOF_SPEC_HINT = re.compile(
    r"破顶[^。\n]{0,30}?(?:缸径|柱径|行程|尺寸|破顶力|调角|摆动|收回|伸出|重量|机构|装置参数)")
# 正文里已经给了具体数量（数字+量词），就不该再判「数量写成模糊说法」。
_DIGIT_UNIT = re.compile(r"\d+(?:\.\d+)?\s*(?:根|组|架|件|套|台|个|只)")
# 材质/涂装要分两个维度核对：牌号解决「材质」，漆层/颜色/镀层解决「涂装」。
# 合成一条白名单会两头判错：只写 Q690 的对照表章被判「涂装没落到具体值」，
# 只写「一层底漆两层面漆」的章又能替材质过关。
_MATERIAL_WORDS = re.compile(r"材质|牌号|钢材|钢板|型材|棒材")
_COATING_WORDS = re.compile(r"涂装|喷漆|喷塑|油漆|涂覆|表面处理|防腐|镀层|镀锌|镀铬|钝化|漆")
_COATING_CONCRETE = re.compile(
    r"底漆|面漆|喷漆|喷塑|镀锌|镀铬|钝化|镗磨|阳极|漆膜|膜厚|[一二两三四五]\s*层"
    r"|银色|白色|红色|颜色[:：]")
# 破顶装置的巷道高度/收回/伸出高度不是「支架高度」，比对设备参数时要排掉这类限定语。
_NON_DEVICE_QUALIFIER = re.compile(r"巷道|破顶|收回|伸出|适用|净高|机面|机身|迎头|层高")


def _describes_roof_device(title: str, body: str) -> bool:
    """本章是在「写破顶装置本身」，还是只是顺带提一句？

    线上事故（第一个标书v7）：6 个失败章节全部栽在「破顶装置关键尺寸缺失」——
    可其中 4 章（项目范围、技术路线、关键技术指标对照、井下支护安全）只是在背景里
    写了「架尾设破顶装置对三角应力区破顶卸压」，本节根本不该展开巷道高度/收回/伸出，
    模型两轮修订也补不进去，只能整章判失败。判定口径因此收窄为：

    1. 标题点名破顶（「破顶装置参数」「破顶机构」）→ 是；
    2. 正文提到破顶 ≥3 次，且至少一处是在讲它的构造/参数（破顶…缸径/柱径/行程/尺寸/破顶力）
       → 是（结构、原理、参数响应这些确实要写全的章节）；
    3. 只是「设破顶装置…破顶卸压」两句带过 → 不是，不再要求写全尺寸。
    """
    if "破顶" in str(title or ""):
        return True
    if str(body or "").count("破顶") < 3:
        return False
    return bool(_ROOF_SPEC_HINT.search(str(body or "")))


_FIELD_PATTERNS = {
    "初撑力": r"初撑力",
    "工作阻力": r"(?:支架)?工作阻力",
    "高度": r"(?:支架高度|支护高度|高度|高)(?!压)",
    "宽度": r"(?:支架宽度|支护宽度|宽度|宽)",
    "长度": r"(?:支架长度|长度|长)(?!期)",
    "重量": r"(?:整组支架重量|支架重量|重量|重)(?!点|新)",
}


def _flat_value(text: str) -> str:
    """数值比对前统一写法：去掉空白，把各种波浪线/长横线折成半角减号
    （正文常写「3.0～3.5m」，原文是「3.0-3.5m」，不归一就会误判成"原文没有"）。"""
    return re.sub(r"\s+", "", str(text or "")).translate(
        str.maketrans({"～": "-", "~": "-", "—": "-", "–": "-", "－": "-", "−": "-"}))


def _mm_range(value: str, unit: str = "m") -> str:
    """「3.0-3.5」+ 单位 → 毫米归一写法「3000-3500」，用于跨单位比对。"""
    scale = 1000 if str(unit or "").lower() == "m" else 1
    numbers = re.findall(r"\d+(?:\.\d+)?", str(value or ""))
    if not numbers:
        return ""
    return "-".join(str(int(round(float(n) * scale))) for n in numbers)


def _mm_tokens(text: str) -> set[str]:
    """文本里所有「数字 + m/mm」按字面单位归一成毫米，用于「3.0-3.5m 写成 3000-3500mm」的比对。"""
    tokens: set[str] = set()
    for match in re.finditer(r"(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)\s*(mm|m)(?![a-zA-Z])",
                             _flat_value(text)):
        tokens.add(_mm_range(match.group(1), match.group(2)))
    return tokens


def _field(name: str) -> str:
    if any(word in name for word in ("运输", "不可拆卸", "最大", "前架", "后架", "底座")):
        return ""
    for field in _FIELD_PATTERNS:
        if field in name:
            return field
    return ""


def _context_level(text: str, default: str = "assembly") -> str:
    markers = list(re.finditer(r"单柱|单根立柱|每根立柱|立柱(?=为|单伸缩|双伸缩|缸径|[0-9]+根|参数|技术参数|与千斤顶|$)|整架|整组|支架整体", text))
    if markers:
        return "column" if markers[-1].group() in ("单柱", "单根立柱", "每根立柱", "立柱") else "assembly"
    return default


def _owned_segments(text: str, registry: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str, str]]:
    aliases: dict[str, dict[str, Any]] = {}
    for device in registry:
        for alias in [device.get("canonical_name", ""), *(device.get("aliases") or [])]:
            if alias:
                aliases[str(alias)] = device
    if not aliases:
        return []
    pattern = re.compile("|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True)))
    owned: list[tuple[dict[str, Any], str, str]] = []
    active: dict[str, Any] | None = None
    active_level = "assembly"
    table_columns: dict[int, dict[str, Any]] = {}
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        hits = list(pattern.finditer(line))
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            mapped = {i: aliases[h.group()] for i, cell in enumerate(cells) if (h := pattern.search(cell))}
            if len(mapped) >= 2 and not re.search(r"\d{3,}", line):
                table_columns = mapped
                active_level = "column" if re.search(r"单柱|立柱参数", line) else "assembly"
                active = None
                continue
            if table_columns and not hits:
                for i, device in table_columns.items():
                    if i < len(cells):
                        owned.append((device, " ".join(cells[:min(table_columns)]) + "：" + cells[i], active_level))
                continue
            if not hits and re.search(r"端头|超前|端尾|中置式", line):
                # 行内提到其他设备但用的是简称/别名之外的写法：归属不明，跳过以免误判。
                active = None
                continue
        else:
            table_columns = {}
        if hits:
            for i, hit in enumerate(hits):
                end = hits[i + 1].start() if i + 1 < len(hits) else len(line)
                # A new explicit device starts at assembly unless the local prefix says single column.
                segment = line[hit.end():end]
                owned.append((aliases[hit.group()], segment, "assembly"))
            if len({aliases[h.group()]["canonical_name"] for h in hits}) == 1:
                active = aliases[hits[-1].group()]
                # Inherit level from headings / introductory labels, not from a mixed numeric sentence.
                if re.match(r"^\s*#{1,6}\s", line) or not re.search(r"\d{3,}", line):
                    active_level = _context_level(line)
                else:
                    active_level = "assembly"
            else:
                active = None
        elif active:
            if re.match(r"^\s*#{1,6}\s", line):
                if re.search(r"立柱|单柱|整架|整组", line):
                    active_level = _context_level(line, active_level)
                else:
                    active = None
            else:
                owned.append((active, line, active_level))
    return owned


def validate_chapter_content(text: str, title: str = "", registry: list[dict[str, Any]] | None = None,
                             source_text: str = "", *, allow_material: bool = False,
                             chapter_number: str = "", strict_completeness: bool = False,
                             roof_in_scope: bool | None = None) -> list[str]:
    """Deterministic high-confidence checks; missing bidder-owned facts remain placeholders.

    chapter_number 是本章的大纲章节号（1 / 1.2 / 2.2）。不传时行为与以前完全一致；
    传了才额外校验正文内嵌小标题的编号体系是否与目录撞号。

    roof_in_scope 决定「破顶装置尺寸必须写全」是否适用：
    * True/False —— 由调用方（生成管线）按本章的目录路径与写作要点判定，
      并保证同一判断下已经把破顶装置的原文条款注入写作上下文；
    * None（默认）—— 兼容旧调用：退回正文启发式，见 _describes_roof_device。
    这样「要求」和「依据」是同一个判断的两面：模型没拿到原文，就不因没写而判失败。
    """
    body = str(text or "")
    errors: list[str] = []
    if not allow_material and len(substantive_text(body)) < 50:
        errors.append("正文有效内容不足50字（空白、仅标题或占位框架），必须重新生成完整正文")
    if chapter_number:
        errors.extend(inline_numbering_issues(body, chapter_number))
    source_flat = _norm(source_text)
    devices = registry or []
    placeholders = re.findall(r"\{\{待填[:：]([^}]+)\}\}", body)
    for field in dict.fromkeys(placeholders):
        # 招标人自己的信息不该由投标人"待填"：原文给出就引用，没给出写「由招标人指定」。
        # 旧口径正是让人写 {{待填:招标人现场负责人及电话}}，实测一份技术标出现 24 处，
        # 既增加形式评审风险，也让正文读起来像在要求甲方自报信息。
        # 「业绩N合同甲方」是历史项目的采购人，不是本次招标人：它属于投标人应提供的
        # 业绩证明材料，留待填是正常的，不能按「招标人自身信息」判错。
        if re.search(r"招标人|采购人|甲方|使用单位|招标方", field) and not re.search(
            r"合同甲方|业绩|业主|用户单位|历史项目", field
        ):
            errors.append("招标人自身信息不得留待填：" + field
                          + "（原文给出的直接引用，未给出的写「由招标人指定」）")
            continue
        known = False
        if devices and re.search(r"设备数量|支架数量|采购数量|招标.*(?:值|参数)|初撑力|工作阻力|立柱规格", field):
            known = True
        if re.search(r"结构钢|钢材牌号|顶梁材质|柱鞋材质|导向机构材质|护帮板材质", field):
            known = bool(re.search(r"Q\d{3}", source_text))
        if re.search(r"无缝钢管|缸筒材质|立柱材质|千斤顶材质|活塞杆材质|液压缸管材", field):
            known = "27SiMn" in source_text
        if re.search(r"连接件材质|铰接件材质", field):
            known = "30CrMnTi" in source_text
        if re.search(r"销轴材质", field):
            known = "30CrMnTi" in source_text
        if re.search(r"总工期|项目工期|交付期|交货期|^工期$", field):
            known = bool(re.search(r"\d+日历天", source_flat))

        if re.search(r"^(?:设备|整机|产品|整体)?(?:质保|保修)(?:期|期限|时长)?$", field):
            known = bool(re.search(r"(?:质保|保修)[^。]{0,80}(?:\d+年|\d+个月)", source_flat))
        if re.search(r"标准.*(?:号|编号)|执行标准", field):
            known = bool(re.search(r"(?:GB/T|MT/T|NB/T)\s*\d", source_text))
        # 「其余/其他/未列明」是招标原文没有逐一列明的范围，本来就该由投标人自填；
        # 不能因为原文给过同类材质（比如部分销轴是 30CrMnTi）就强制它有值。
        # 实测「其余销轴材质」被判「招标原文已有依据，不得使用待填」，
        # 模型按"不得编造"规则留待填→两轮修订都改不掉→整章生成失败。
        if re.search(r"其余|其他|未列明|未注明|未标注|另(?:行|定)|待定", field):
            known = False
        if known:
            errors.append("招标原文已有依据，不得使用待填：" + field)
    # 偏差/偏离章节必须只写「无偏离」。招标文件格式9/10 只要求"无偏差时在本页写无"，
    # 主动罗列偏离条目等于替评委把「重大偏差」条款（20.1④、负偏离即废标）摆上台面。
    # 实测生成的《偏离项说明与替代措施》写了偏离判定原则与登记表，属自伤式内容。
    if re.search(r"偏离|偏差", str(title or "")):
        if not re.search(r"无(?:负)?偏离|完全响应|无偏差", body):
            errors.append("偏差/偏离章节只作无偏离声明：须明确写出「无负偏离、完全响应」，不得罗列偏离条目")
        elif re.search(r"(?:偏离项|偏差项)\s*[:：]|负偏离[:：]|偏离内容[:：]", body):
            errors.append("偏差/偏离章节出现具体偏离条目，须改为无偏离声明并删除条目")
    # 数量口径：招标原文给了具体根数（2 根/组、4 根/组），写成「按组配套」就等于没响应。
    # 只抓「数量/配置」紧邻的模糊写法：早期版本对整个正文里的「若干」「详见附表」判错，
    # 把「供货时提供」类正文里的正当引用也判成问题，导致整章生成失败（实测 seq10 两轮都失败）。
    # 现在逐句判定，并放过两类正当写法：(a) 表格行里的口径 —— 表格本身已经列了数字；
    # (b) 同一句里已经写了「数字+量词」（如「立柱 20 根/组」），只是顺带提一句清单。
    # 线上事故：「推移千斤顶按整组配置、立柱 20 根/组」的表格章被判「数量写成模糊说法」，
    # 6 个失败章节里排第 2 位，其实是误伤。
    _vague_qty = re.compile(r"按组配套|按套配置|按套配备")
    _qty_pointer = re.compile(r"(?:数量|配置|配[备齐])[^。\n|]{0,12}(?:若干|见清单|详见清单|详见附表)")
    # 章节里已经有「数字+量词」的表格行（如「| 1 | 端头联排液压支架 | 1 组 |」），
    # 说明数量口径已经落到表里，正文再写「详见附表」属于正当引用，不算模糊。
    _has_qty_table = any(_DIGIT_UNIT.search(ln) for ln in body.splitlines() if "|" in ln)
    for _sentence in re.split(r"(?<=[。；！？\n])",
                              "\n".join(ln for ln in body.splitlines() if "|" not in ln)):
        if not _sentence.strip():
            continue
        if _DIGIT_UNIT.search(_sentence):
            continue
        if not (_vague_qty.search(_sentence)
                or (not _has_qty_table and _qty_pointer.search(_sentence))):
            continue
        errors.append("数量必须写原文数字（如「2 根/组」），不得写「按组配套」「若干」「见清单」")
        break
    # 随机备件清单完整性：原文 24 项只列 12 项属于实质性漏项（备件是价格与响应的组成部分）。
    # 只在「确实列了清单表」的正文上判定：要有带序号的表格行（`| 1 | 立柱 | …`），
    # 且已经列出了 ≥3 个清单条目（说明这一章就是在列清单，不是顺带提一句）。
    # 早期按关键词判定，把「项目组织架构」「培训内容与教材」等 16 章全部误判拦下。
    if source_text and re.search(r"随机备件|备品备件|备件明细", body) and _BILL_ROW.search(body):
        from zhibiao.core.bill_of_materials import spare_table_gaps
        # 逐份清单核对，缺一项就报：旧版按「漏项数 ≥ max(3, 总数×30%)」判，
        # 24 项漏 1 项（U型卡专用工具）、端尾表跳号漏 1/3/5 三项都够不到阈值，全被放行。
        gaps = spare_table_gaps(source_text, body)
        if gaps:
            errors.append("随机备件清单不完整：" + "；".join(gaps[:3])
                          + "；须按招标原文逐项列出（序号、名称、规格型号、单位、数量）")
    # 材质牌号必须与原文一致：实测把销轴牌号 30CrMnTi 写成 30CrMnSi（材料变了，属于
    # 实质性偏离），而牌号写错在通篇文字里几乎看不出来。这里只做「牌号是否出现在原文」
    # 的硬核对，成本极低；原文引用多个牌号时任一命中即可。
    if source_text:
        flat_source = re.sub(r"\s+", "", str(source_text))
        source_grades = set(_GRADE.findall(flat_source))
        # 原文里一个牌号都没有（或原文本身没解析出来）时无从核对，跳过：
        # 实测新2 的招标原文解析后不含牌号，硬核对会把 Q345B/Q235A/40Cr 全部误判。
        if len(flat_source) >= 800 and source_grades:
            for grade in set(_GRADE.findall(body)):
                if grade not in flat_source:
                    errors.append(f"材质牌号「{grade}」不在招标原文中，必须按原文牌号写（不得改写或编造）")
    # 原文已明确规定材质/涂装（牌号、镀层、颜色、漆层数）时，不得用「按招标人要求确定」
    # 「按原文规定选材」这类空话顶替 —— 实测 2.2.3 与涂装段落就是这么写的，等于没响应。
    if source_text and _EXPLICIT_SPEC.search(str(source_text)) and _VAGUE_SPEC.search(body):
        errors.append("招标原文已明确材质/涂装要求（牌号、镀锌钝化、镀铬、漆层与颜色等），"
                      "必须照抄具体值，不得写「按招标人要求确定」「按原文规定选材」")
    # 特殊装置（破顶装置等）的数值必须取自原文：这类表（第58页）常被漏掉，
    # 实测破了顶却缺「巷道高度 3.0-3.5m、收回 2.7m、伸出 4.0m、缸径/柱径 320/290」。
    # 只核对**紧跟在破顶相关字段后**的带单位数值；否则会把段落里无关的数字（压力、日期）
    # 也算进来，实测 15 章被误判。
    if source_text and "破顶" in body:
        from zhibiao.core.spec_clauses import section_for
        section = section_for(str(source_text), "破顶", 4000)
        if section:
            flat_section = _flat_value(section)
            section_mm = _mm_tokens(section)
            invented = []
            for match in _ROOF_VALUE.finditer(body):
                raw = match.group(1) or match.group(2) or ""
                token = _flat_value(raw)
                if not token:
                    continue
                if token in flat_section:
                    continue
                # 正文把 3.0-3.5m 写成 3000-3500mm 也是照原文，不能判成编造。
                if match.group(1) and _mm_range(raw, match.group(3)) in section_mm:
                    continue
                invented.append(raw.strip())
            if invented:
                errors.append("破顶装置数值不在招标原文：" + "、".join(list(dict.fromkeys(invented))[:6])
                              + "；须照原文写（适用巷道高度 3.0-3.5m、收回 2.7m、伸出 4.0m、"
                                "破顶立柱缸径/柱径、破顶力、调角千斤顶缸径/杆径/行程）")
    # 生成时（strict_completeness）还要求"写全"：只避免写错不够，实测 2.2.6 / 3.2.3
    # 通篇讲破顶装置，却一个关键尺寸都没写 —— 漏项和错项一样是负偏离。
    # 是否适用由 roof_in_scope 决定（调用方据目录路径/写作要点判定，并同步注入了原文条款）；
    # 旧调用不传时退回正文启发式，见 _describes_roof_device。
    _roof_required = (_describes_roof_device(str(title or ""), body)
                      if roof_in_scope is None else bool(roof_in_scope))
    if strict_completeness and source_text and _roof_required:
        from zhibiao.core.spec_clauses import section_for
        section = section_for(str(source_text), "破顶", 4000)
        required = []
        if section:
            flat_section = _flat_value(section)
            for match in re.finditer(r"(?:巷道高度|收回后?高度|完全伸出后?高度)"
                                     r"[^。\n]{0,12}?(\d+(?:\.\d+)?(?:\s*[-~～—至]\s*\d+(?:\.\d+)?)?)\s*m",
                                     section):
                value = _flat_value(match.group(1))
                if value and value in flat_section:
                    required.append(match.group(1).strip())
        flat_body = _flat_value(body)
        body_mm = _mm_tokens(body)
        missing = [value for value in dict.fromkeys(required)
                   if _flat_value(value) not in flat_body and _mm_range(value, "m") not in body_mm]
        if missing:
            errors.append("破顶装置关键尺寸缺失：" + "、".join(missing)
                          + "m；讲到破顶装置必须写明适用巷道高度与收回/伸出高度（照原文）")
    # 同理：讲到材质与涂装就必须落到原文的具体值，不能只写"按招标人要求确定"。
    # 材质与涂装分开核对，缺哪个报哪个（见 _MATERIAL_WORDS 处的说明）。
    # 只对「结构/部件/材质/工艺/制造/涂装」类章节生效：实测「关键岗位人员配备」章里
    # 模型写出小标题「岗位人员材质与涂装专业能力要求」，就因为在人员章里提到"材质"
    # 被要求补 Q235 牌号——这是章题跑偏，不该由材质规则来兜。
    _material_scope = not re.search(r"人员|岗位|培训|组织|职责|能力|考核|团队", str(title or ""))
    if strict_completeness and source_text and _material_scope:
        _src = str(source_text)
        _missing_specs: list[str] = []
        if _GRADE.search(_src) and _MATERIAL_WORDS.search(body) and not _GRADE.search(body):
            _missing_specs.append("材质牌号")
        if re.search(r"底漆|面漆|喷漆|喷塑|镀锌|镀铬|钝化|镀层|漆膜|颜色", _src) \
                and _COATING_WORDS.search(body) and not _COATING_CONCRETE.search(body):
            _missing_specs.append("涂装/镀层/颜色")
        if _missing_specs:
            errors.append("材质与涂装未落到具体值（缺：" + "、".join(_missing_specs) + "）："
                          "原文已规定漆层/颜色/镀层/牌号的，必须写出具体内容（如一层底漆两层面漆、"
                          "面漆白色和红色、销轴镀锌钝化、销轴 30CrMnTi/40Cr）")
    # 多设备参数不同，不得用一个"均/统一"的写法带过：实测把三类支架的最大运输外形尺寸
    # 统一写成 6*0.6*0.3m，而端尾支架原文是 4850*1770*800mm（罐笼与巷道过不去就是废标）。
    # 只在**同一句话**里既有「均/统一」又点了该参数名、且指的是多台设备时才判。
    if devices:
        for sentence in re.split(r"[。；\n]", body):
            if not _SHARED_CLAIM.search(sentence):
                continue
            if not _MULTI_DEVICE.search(sentence):
                continue
            differing = []
            for name in ("最大运输外形尺寸", "支架长度", "整组支架重量", "支架重量"):
                if name not in sentence:
                    continue
                values = set()
                for device in devices:
                    for param in device.get("parameters") or []:
                        if _norm(str(param.get("name") or "")) == _norm(name):
                            values.add(re.sub(r"\s+", "", str(param.get("raw") or param.get("value") or "")))
                values.discard("")
                if len(values) >= 2:
                    differing.append(name + "（" + " / ".join(sorted(values)) + "）")
            if differing:
                errors.append("各设备该参数取值不同，不能统一写同一个值，须按设备分别写明：" + "；".join(differing))
                break
    for device, segment, inherited_level in _owned_segments("# " + title + "\n" + body, devices):
        for name, pattern in _FIELD_PATTERNS.items():
            # Only direct field-to-value assertions. Never jump over other field names/prose to a later number.
            link = r"[\s|:：为是≥≤=约不低于达到不少（）()]*"
            unit_before = r"(?:(?:kN|mm|m|吨|t)[）)]?[\s|:：]*)?"
            value = r"(\d+(?:\.\d+)?(?:\s*[-~～—至]\s*\d+(?:\.\d+)?)?)\s*([kK][nN]|mm|m|吨|[tT]|[kK][gG])?"
            for match in re.finditer(pattern + link + unit_before + value, segment):
                prefix = re.split(r"[。；;，,]", segment[:match.start()])[-1]
                assertion = re.split(r"[。；;]", segment)[0] if match.start() == 0 else prefix
                if re.search(r"不得|不应|避免|错误|背景|调研|不可拆卸|运输|前架|后架|底座", assertion):
                    continue
                # 破顶装置的巷道高度/收回/伸出高度不是「支架高度」。实测正文照原文字面写了
                # 「巷道高度 3.0-3.5m、收回 2.7m、伸出 4.0m」，却被判成「中置式大阻力端尾支架
                # 的高度写成 3.0-3.5m，应按 1700～3500mm」——这条与「破顶尺寸必须写全」互相打架，
                # 两轮修订必然在同一处来回，整章只能判失败。带这些限定语就跳过。
                if _NON_DEVICE_QUALIFIER.search(segment[max(0, match.start() - 8):match.start()]):
                    continue
                level = _context_level(segment[:match.start()], inherited_level)
                # 同一台设备的参数可能有两套：整架（assembly）与单柱/立柱（column）。
                # 层级全靠句子里的「立柱/整架」字样猜：标注写在数值之后、写在表格列头、
                # 或用了简称，都会猜错。猜错就会拿整架值去比单柱值，结果必然不匹配 ——
                # 实测同一章在「502 应按整架参数表 10048」和「10048 应按单柱参数表 502」
                # 之间来回判错，两轮修订仍在同一处，整章被判「正文质量未通过」。
                # 所以：只要数值与该设备**任一层**的原值一致就放行；全都不一致才是真错
                # （例如 784 不属于端头支架任何一层，仍然报错）。
                params = [x for x in device.get("parameters") or []
                          if _field(str(x.get("name") or "")) == name]
                if not params:
                    continue
                expected = [x for x in params if x.get("level", "assembly") == level] or params
                suffix = segment[match.end():match.end() + 5]
                if re.match(r"\s*(?:MPa|%|根|天|日)", suffix, re.I):
                    continue
                unit = match.group(2) or ""
                if name in ("初撑力", "工作阻力") and unit and unit.lower() != "kn":
                    continue
                if name in ("高度", "宽度", "长度") and unit and unit not in ("m", "mm"):
                    continue
                if name == "重量" and unit and unit.lower() not in ("kg", "t", "吨"):
                    continue
                actual = _number(match.group(1))
                values = {_number(str(item.get("value") or "")) for item in params}
                values.discard("")
                # Equal force values across two devices do not make their pressure conditions interchangeable.
                pressure = re.match(r"\s*[（(]?\s*[Pp]\s*=\s*([0-9.]+)\s*MPa", segment[match.end():], re.I)
                pressure_values = {str(x.get("pressure", {}).get("value") or "") for x in params if isinstance(x.get("pressure"), dict)}
                pressure_values.discard("")
                if pressure and pressure_values and _number(pressure.group(1)) not in {_number(x) for x in pressure_values}:
                    errors.append(f"{device['canonical_name']}的{name}压力条件P={pressure.group(1)}MPa与该设备原表不一致，应为" + "/".join(sorted(pressure_values)) + "MPa")
                if actual in values or not values:
                    continue
                equivalent = False
                for item in params:
                    expected_unit = str(item.get("unit") or "")
                    expected_number = _number(str(item.get("value") or ""))
                    if unit == "mm" and expected_unit.startswith("m") and not expected_unit.startswith("mm"):
                        converted = "-".join(str(float(x) * 1000).removesuffix(".0") for x in expected_number.split("-"))
                        equivalent |= actual == converted
                    if unit == "m" and expected_unit.startswith("mm"):
                        converted = "-".join(str(float(x) / 1000).removesuffix(".0") for x in expected_number.split("-"))
                        equivalent |= actual == converted
                if not equivalent:
                    errors.append(f"{device['canonical_name']}的{name}写成{match.group(1)}{unit}，应按该设备参数表：" + " / ".join(str(x.get("raw") or x.get("value")) for x in expected))
    # Two different names for one purchased object must not be summed as extra items.
    for device in devices:
        if "端尾" not in str(device.get("canonical_name") or ""):
            continue
        for line in body.splitlines():
            if re.search(r"中置式[^。\n]{0,70}(?:2|两)架[^。\n]{0,40}(?:[、，,；;]|和|及)\s*端尾支架\s*(?:[（(]?\s*)?(?:2|两)架", line):
                errors.append("端尾支架是中置式大阻力端尾支架的简称，不能作为第四种设备重复采购")
    # 参数响应对照表被截断：模型常列到第 8 项就把「完全响应，无偏离」写进最后一列当收尾，
    # 后面的泵站压力/移架步距/电液控制/整组重量/运输尺寸/不可拆卸件重量全丢（V7 的 3.1.4、4.1.4）。
    # 三条同时满足才判，避免误伤只讲某几个参数的章节：
    #   a) 本章标题属于「参数响应/参数对照」类；b) 该设备的参数在正文已出现 ≥3 个；c) 仍有缺项。
    # 部件级章节（立柱参数、千斤顶参数、销轴材质…）本来就只覆盖部件，不做整机完整性判定。
    if strict_completeness and devices and not re.search(
            r"立柱|千斤顶|销轴|部件|结构件|胶管|阀", str(title or "")) and re.search(
            r"参数\s*(?:响应|对照|表)|指标\s*对照|逐条\s*响应|响应\s*对照", str(title or "")):
        _flat_body = _norm(body)
        _scope = str(title or "") + body
        for device in devices:
            _canonical = str(device.get("canonical_name") or "")
            _alias = re.sub(r"^(?:中置式大阻力|中置式)", "", _canonical)
            # 设备名多半只出现在标题里（表里只写参数项），标题也要算进范围
            if not _canonical or not (_canonical in _scope or (_alias and _alias in _scope)):
                continue
            _labels: list[str] = []
            _missing: list[str] = []
            for param in device.get("parameters") or []:
                label = str(param.get("name") or "").strip()
                if not label or label in _labels:
                    continue
                _labels.append(label)
                if _norm(label) in _flat_body:
                    continue
                raw = str(param.get("raw") or param.get("value") or "").strip()
                _missing.append(f"{label}（{raw}）" if raw else label)
            if len(_labels) >= 4 and len(_labels) - len(_missing) >= 3 and _missing:
                errors.append(f"{_canonical}参数响应表未列全：只列了 {len(_labels) - len(_missing)}/{len(_labels)} 项，缺 "
                              + "、".join(_missing[:8])
                              + (" 等" if len(_missing) > 8 else "")
                              + "；须逐项列出参数行，不得用「完全响应，无偏离」代替参数行")
    # 千斤顶/立柱的数量与推拉力不得用「—」占位：原文给了「2 根/组」「158/69kN」，
    # 写成「—」等于没响应（V7 的 2.3.4、3.1.2、3.1.3、3.3.3、4.2.2～4.2.4、10.3.1、11.2.1）。
    # 只在「该行除了名称以外一个数字都没有」时判，避免误伤原文本就没给的推力格。
    if source_text:
        from zhibiao.core.bill_of_materials import extract_part_quantities
        _known_qty = {row["name"] for row in extract_part_quantities(source_text) if row.get("qty")}
        _dashed: list[str] = []
        if _known_qty:
            for line in body.splitlines():
                if not re.search(r"千斤顶|立柱", line):
                    continue
                if not re.search(r"(?:^|\|)\s*(?:—|–|－|-|/|无)\s*(?=\|)", line):
                    continue
                rest = re.split(r"千斤顶|立柱", line)[-1]
                if re.search(r"\d", rest):
                    continue
                hits = [name for name in _known_qty if name in line]
                if hits:
                    _dashed.extend(hits)
        if _dashed:
            errors.append("千斤顶/立柱的数量与推拉力不得写「—」："
                          + "、".join(list(dict.fromkeys(_dashed))[:6])
                          + " 原文已给出数量（如「2 根/组」），须照原文填写")
    # 引用招标原文之外的国标属于正常写法；只有与原文同一标准号但版本年号不一致才算冲突。
    if source_text and re.search(r"(?:GB/T|MT/T|NB/T)", source_text):
        source_years: dict[str, set[str]] = {}
        for prefix, number, year in re.findall(r"(GB\s*/\s*T|MT\s*/\s*T|NB\s*/\s*T)\s*(\d+(?:\.\d+)?)(?:\s*[-—]\s*(\d{4}))?", source_text):
            source_years.setdefault(_norm(prefix + number), set()).add(year)
        for standard in set(re.findall(r"(?:GB\s*/\s*T|MT\s*/\s*T|NB\s*/\s*T)\s*\d+(?:\.\d+)?(?:\s*[-—]\s*\d{4})?", body)):
            parts = re.match(r"(GB\s*/\s*T|MT\s*/\s*T|NB\s*/\s*T)\s*(\d+(?:\.\d+)?)(?:\s*[-—]\s*(\d{4}))?", standard)
            if not parts:
                continue
            key = _norm(parts.group(1) + parts.group(2))
            known_years = {year for year in source_years.get(key, set()) if year}
            if known_years and parts.group(3) and parts.group(3) not in known_years:
                errors.append("标准号版本与招标原文不一致：" + standard + "（原文为" + "/".join(sorted(known_years)) + "版）")
    # Final handover must retain the agreed baseline; intermediate milestones may be earlier.
    durations = re.findall(r"(?:总工期|交货期|工期要求|合同签订后)[^。\n]{0,50}?(\d+)\s*日历天", source_text)
    if durations:
        days = durations[0]
        for match in re.finditer(r"(?:总工期|项目总工期|合同工期)[^\d。\n]{0,12}(\d+)\s*(?:日历天|天|日)", body):
            if match.group(1) != days:
                errors.append(f"项目总工期写成{match.group(1)}天，与招标原文{days}日历天不一致")
        for line in body.splitlines():
            if re.search(r"验收交付|最终交付|项目交付|整体交付", line) and "提前" not in line:
                final_days = re.findall(r"第\s*(\d+)\s*天", line)
                # 早于招标工期交付属于更优承诺，只有超出招标基准才提示；不得把提前交付判为矛盾。
                if final_days and max(map(int, final_days)) > int(days):
                    errors.append(f"最终验收交付安排在第{max(map(int, final_days))}天，与招标{days}日历天的基准计划不一致；不能把中间节点写成全项目交付")
    # 表格结构/技术要求条目/边界条款/部件参数/数量口径（completeness 模块，纯函数）。
    # 只在生成时判：存量正文导出不套这条，避免历史项目一夜之间全部导不出去。
    if strict_completeness:
        from zhibiao.core.completeness import (
            boundary_gaps,
            component_gaps,
            quantity_gaps,
            scope_column_gaps,
            table_structure_gaps,
            tech_item_gaps,
        )
        errors.extend(table_structure_gaps(body))
        errors.extend(quantity_gaps(body))
        if source_text:
            errors.extend(scope_column_gaps(body))
            errors.extend(tech_item_gaps(source_text, body, title))
            errors.extend(boundary_gaps(source_text, body, title))
            errors.extend(component_gaps(source_text, body, title))
    # 招标文件已写死的事实被留成待填 / 被写成别的身份：都是评审当场能看出来的硬伤，
    # 且完全可以机械判定，不需要模型再判断一次。
    errors.extend(_frozen_fact_issues(body, title, str(source_text or "")))
    # 章末半句断尾（「我方按合同条款」直接结束）：明显没写完，交给续写/重试修。
    truncation = _truncation_issue(body)
    if truncation:
        errors.append(truncation)
    return list(dict.fromkeys(errors))


# ── 招标文件已写死的事实（禁止待填 / 禁止改身份 / 禁止漏查询渠道）────────────────
_AGENT_BAN = re.compile(r"不接受\s*代理(?:商|机构|投标)|不允许\s*代理|不得\s*代理|代理商投标\s*[：:]?\s*不接受")
_AGENT_MENTION = re.compile(r"(?:授权)?代理商|代理机构|代理商投标")
_AGENT_NEGATED = re.compile(r"(?:不接受|不允许|不得|不设|无需|非)[^。\n|]{0,12}代理")
# 招标原文里给出的固定地点/单位（如「代储至机电设备管理中心配件库」）
_FROZEN_PLACE = re.compile(r"代储至([^。；\n]{4,40}?)[。；\n]")
# 信誉查询渠道：招标文件里点名的查询网站，正文一处都不能漏
_CREDIT_SITES = ("国家企业信用信息公示系统", "信用中国", "中国执行信息公开网",
                 "全国法院失信被执行人名单信息公布与查询平台")


def _frozen_fact_issues(body: str, title: str, source_text: str) -> list[str]:
    issues: list[str] = []
    flat_source = re.sub(r"\s+", "", source_text)

    # ① 代储地点：招标原文写明「代储至…配件库」，正文却留 {{待填:…地点}}
    if source_text:
        places = [p.strip() for p in _FROZEN_PLACE.findall(flat_source) if len(p.strip()) >= 6]
        if places:
            place = places[0]
            if re.search(r"\{\{待填[:：][^}]*(?:代储|地点|地址|单位)[^}]*\}\}", body) and place not in re.sub(r"\s+", "", body):
                issues.append("招标原文已明确代储地点（%s），不得留待填；须照原文写全" % place[:30])

    # ② 代理商：招标文件明确不接受代理商，正文不得出现「授权代理商」这类身份表述
    if source_text and _AGENT_BAN.search(flat_source):
        for match in _AGENT_MENTION.finditer(body):
            head = body[max(0, match.start() - 16):match.end() + 10]
            if _AGENT_NEGATED.search(head):
                continue          # 「不接受代理商投标」这种否定表述是对的
            issues.append("招标文件明确不接受代理商投标，正文不得出现「%s」的身份表述（应直接写制造商）"
                          % match.group(0))
            break

    # ③ 信誉查询渠道：招标文件点名了几个查询网站，资格审查/信誉章节必须逐个写到
    if source_text and re.search(r"信誉", body) and re.search(r"信誉", flat_source):
        required = [site for site in _CREDIT_SITES if site in flat_source]
        if len(required) >= 2:
            flat_body = re.sub(r"\s+", "", body)
            missing = [site for site in required if site not in flat_body]
            if missing:
                issues.append("信誉要求须逐个写到招标文件点名的查询渠道，本章缺：%s"
                              % "、".join(missing))
    return issues


# 「章末断尾」类质检项的前缀：它们描述的是**结构未完成**（末句/末表没写完），
# 不是内容错误。生成管线必须把它们从「整章作废」的判据里摘出来，先交给收尾续写
# （tail_repair）补；补不上也要保留正文、只在 chapter_source 记一条中性说明。
# 线上事故（项目 bee64b91…，seq=14）：800 字正文只因为末句没句号，被判
# 「正文质量未通过：章末半句断尾（正文未写完，须补全后再落库）」→ content 落成 0，
# 用户拿到的是一章空白。
#
# 第二条来自 `source_fidelity.incomplete_tail`（末字停在「按/及/、/（」等悬空词），
# 与本章 `_truncation_issue` 是同一事实的两种表述，必须按同一档处理。
TAIL_TRUNCATION_ISSUES = ("章末半句断尾", "正文末句未完成")


def is_tail_truncation_issue(issue: str) -> bool:
    """这条质检项是不是「章末断尾」类（可续写、不该整章作废）。"""
    text = str(issue or "").strip()
    return bool(text) and text.startswith(TAIL_TRUNCATION_ISSUES)


def _truncation_issue(body: str) -> str:
    """章末是否半句断尾。只看机械信号，不评判措辞。

    章末的插图占位框（`{{图片占位:…}}`）是平台有意预留的图位，不是「没写完的半句」：
    旧实现拿它当最后一行判断，一份正文完整的标书会被判「章末半句断尾」而导不出去。
    """
    lines = [line.strip() for line in str(body or "").splitlines() if line.strip()]
    while lines and is_image_slot_line(lines[-1]):
        lines.pop()
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) < 40:
        return ""
    tail = lines[-1]
    if not tail or tail.startswith("|") or tail.endswith("|"):
        return ""
    # 列表型正文（「- xxx」「1. xxx」）按条目结束，不算断尾：清单本来就是一条一条的
    if re.match(r"^(?:[-*·•]|\d+[.、)）])\s*", tail):
        return ""
    if re.search(r"(?:签字|签章|盖章|公章|年\s*月\s*日|日期[:：]).{0,24}$", tail):
        return ""
    if re.search(r"[。！？；：”』）】]$|[|]$", tail):
        return ""
    if re.search(r"(?:\d+(?:\.\d+)?\s*(?:mm|m|kW|t/h|MPa|kg|年|天|个月|小时|%)|[一二三四五六七八九十]+(?:年|天|个月))$", tail):
        return ""
    return "章末半句断尾（正文未写完，须先续写补齐；补不上按现有内容保留）：%s" % tail[-24:]


def quality_repair_prompt(title: str, body: str, issues: list[str]) -> str:
    return (f"请修复《{title}》正文中的确定性质量问题。\n"
            "只按共享台账中的原始招标证据修复；保持本节范围、编号及其他正确内容。\n"
            "不得把不同设备的参数调包；端尾简称不得重复计数；整架与单柱不可混用。\n"
            "技术参数表优先于背景介绍；已有真实参数、工期、质保、材质、标准不得写待填。\n"
            "投标人未提供的姓名/证书/报价/联系人等保持待填，不得编造。\n"
            "若正文为空或仅有标题，重新生成不少于200字的实质正文；否则最小范围修订，不能删短整章。\n"
            "待修复问题：\n" + "\n".join("- " + i for i in issues[:25])
            + "\n\n【待修订正文】\n" + str(body or "（空正文）") + "\n\n只输出完整修订后正文。")
