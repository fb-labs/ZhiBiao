"""商务硬约束：抽取、注入与正文口径校验。

招标文件里有一批**一票否决**的商务口径（前附表/投标人须知）：
投标有效期、投标保证金、采购预算、工期（交货期）、质保期（含易损件）、交货地点、付款方式。
它们和 20.1「重大偏差」、格式9/10「付款方式和交货期、技术偏差出现负偏离即废标」
直接挂钩，而此前生成链路只抽取技术参数，完全没看这些条款 ——
实测导出的技术标正文通篇没有承诺「投标有效期 90 天」，
工期、质保期也只在对的地方偶然写对，一旦某章写成 60 天 / 24 个月就是负偏离。

本模块只做三件事：从原文抽值、渲染成注入用的基线、校验正文是否写出相反口径。
不猜、不补：原文没有的字段一律不出现。
"""

from __future__ import annotations

import re

_FORMAT_ITEMS = (
    ("投标函", "投标函（须含投标有效期承诺）"),
    ("开标一览表", "开标一览表"),
    ("投标报价", "投标报价表/分项报价表"),
    ("主要外购配套件", "主要外购配套件明细表"),
    ("商务偏差", "商务偏差表（无偏差须写「无」）"),
    ("技术规格偏差", "技术规格偏差表（无偏差须写「无」）"),
    ("投标保证金", "投标保证金凭证/保函"),
    ("授权委托书", "法定代表人授权委托书"),
    ("营业执照", "营业执照"),
    ("失信", "失信被执行人/严重违法失信查询结果"),
)


_OWNER_PH = re.compile(r"\{\{\s*待填\s*[:：]\s*((?:招标人|采购人|甲方|使用单位|招标方)[^}]{0,30})\}\}")


def neutralize_owner_placeholders(text: str) -> tuple[str, int]:
    """把「招标人侧信息」的待填占位改成「由招标人指定」。

    旧口径要求写 {{待填:招标人现场负责人及电话}}，存量正文里已经有一批（实测某项目
    27 章带该占位）。这些占位既无法由投标人填写，又是形式评审风险，所以统一兜底改写：
    前一个字已经是「由」时只补「招标人指定」，避免出现「由由招标人指定」。

    只处理招标人/采购人一侧；投标人自己的姓名、证书、业绩、报价仍保留待填。
    """
    if not text:
        return text, 0
    count = 0

    def sub(match: "re.Match[str]") -> str:
        nonlocal count
        count += 1
        before = match.string[:match.start()].rstrip("（(，,；; ")
        # 「由｜按｜依据｜根据」后面直接接名词，不能再补「由」
        return "招标人指定" if before[-1:] in "由按据照依" else "由招标人指定"

    return _OWNER_PH.sub(sub, text), count


def submission_checklist(tender_text: str) -> list[dict[str, str]]:
    """列出招标文件要求、但纯技术标正文通常不含的递交要件。

    招标文件的初步评审里有「投标文件组成 是否完整且符合招标文件要求」这一条，
    技术标再完美，缺投标函/报价表/偏差表/保证金凭证一样过不了形式评审。
    这里只做清单提醒，不阻断导出：正文是技术标，这些附件本来就要另外准备。
    """
    flat = _flat(tender_text)
    items: list[dict[str, str]] = []
    for keyword, label in _FORMAT_ITEMS:
        if keyword in flat:
            items.append({"item": label, "why": f"招标文件要求（含「{keyword}」）"})
    if "投标有效期" in flat:
        items.append({"item": "投标有效期承诺书", "why": "响应性评审项：投标有效期是否符合要求"})
    return items


def _flat(text: str) -> str:
    """去空白但不合并换行，便于用「行内窗口」取数。"""
    return re.sub(r"[ \t\u3000]+", "", str(text or ""))


def extract_commercial_terms(text: str) -> dict[str, str]:
    """从招标原文抽取商务硬约束。抽不到就不放进结果，绝不填默认值。"""
    flat = _flat(text)
    terms: dict[str, str] = {}

    match = re.search(r"投标有效期[^。；\n]{0,40}?(\d{2,3})\s*(?:个)?(日历天|天)", flat)
    if match:
        terms["validity"] = f"{match.group(1)}{match.group(2)}"

    match = re.search(r"投标保证金[^。；\n]{0,100}?(?:人民币)?([0-9][0-9,]*(?:\.\d+)?)\s*元", flat)
    if match:
        amount = match.group(1).replace(",", "")
        terms["bond"] = amount + "元"
        terms["bond_wan"] = _as_wan(amount)

    match = re.search(r"(?:采购预算|预算金额|最高限价|控制价)[^。；\n]{0,20}?([0-9]+(?:\.[0-9]+)?)\s*(万元|元)", flat)
    if match:
        terms["budget"] = match.group(1) + match.group(2)
        terms["budget_value"] = _scaled(match.group(1), match.group(2))

    # 工期：优先「合同签订后N日历天」，其次「交货期/工期/服务期：N天」
    match = re.search(r"(?:合同生效后|合同签订后|合同签定后)[^。；\n]{0,12}?(\d+)\s*(?:个)?(日历天|天|工作日)", flat)
    if not match:
        match = re.search(r"(?:交货期|工期|服务期|供货期)[^。；\n]{0,20}?[：:]?\s*(\d+)\s*(?:个)?(日历天|天|工作日)", flat)
    if match:
        terms["duration"] = f"{match.group(1)}{match.group(2)}"

    match = re.search(r"质保期[^。；\n]{0,50}?(\d+)\s*个?月", flat)
    if match:
        terms["warranty_months"] = match.group(1)
    match = re.search(r"(?:易损件|高压胶管|密封件)[^。；\n]{0,60}?(\d+)\s*个?月", flat)
    if match:
        terms["wear_months"] = match.group(1)

    match = re.search(r"交货地点[^。；\n]{0,40}?[：:]\s*([^。；\n]{2,40})", flat)
    if match:
        terms["place"] = match.group(1).strip()

    payment, ratios = _payment_clause(flat)
    if payment:
        terms["payment"] = payment
    if ratios:
        terms["payment_ratios"] = "/".join(ratios)
    terms.update(extract_owner_contact(text))
    return terms


def extract_owner_contact(text: str) -> dict[str, str]:
    """抽出**招标人**自己的联系人/电话/邮箱。

    为什么必须抽：规则里写着「现场负责人与应急电话在招标文件中没有明确时
    统一写 {{待填:招标人现场负责人及电话}}」，但原文其实一直在「九、联系方式」
    里给出（实测联系人赵先生、电话0356-5828565），只是没被抽取过 ——
    结果一份技术标里出现 24 处「招标人现场负责人及电话」待填，
    既是形式评审（投标文件组成/格式）风险，也让正文读起来像在要求招标人自报信息。

    只看招标人/采购人所在的那一段，遇到「招标代理机构」就停，
    避免把代理机构的联系人（晋女士、常女士）当成招标人联系人。
    """
    lines = [re.sub(r"[ \t\u3000]+", "", line) for line in str(text or "").splitlines()]
    # 前附表里也会出现「招标人：…」但没有联系方式，要逐个候选块试，取第一个真正有联系人的
    starts = [i for i, line in enumerate(lines)
              if re.match(r"^(?:招标人|采购人|招标单位|采购单位)[：:]", line)]
    for start in starts:
        found: dict[str, str] = {}
        for line in lines[start:start + 12]:
            if re.match(r"^(?:招标代理机构|代理机构)[：:]", line):
                break
            for key, pattern in (("owner_contact", r"^(?:联系人|项目联系人)[：:]([^\s，,。；;]{2,20})"),
                                 ("owner_phone", r"^(?:电话|联系电话|联系方式)[：:]([0-9\-－—－\s]{7,24})"),
                                 ("owner_email", r"^(?:电子邮箱|邮箱)[：:]([^\s，,。；;]{3,40})")):
                match = re.match(pattern, line)
                if match and key not in found:
                    found[key] = match.group(1).strip()
        if found.get("owner_contact") or found.get("owner_phone"):
            return found
    return {}


def _payment_clause(flat: str):
    """取付款条款正文与其中出现过的比例。

    直接找"真正写了比例/质保金"的付款句子，而不是从「付款方式」锚点往后截一段：
    合同模板里"技术开发经费总额为人民币：____万元"这类空格会把截断取数带偏
    （实测抽出来是一堆模板空白），而比例句（…支付合同价款的30%…）才是要响应的口径。

    PDF 抽取会把同一句拆到多行（"…出具竣工验收报告，支" / "付合同价款的30%；"），
    所以先剔除页眉页脚，再把换行拼掉，最后按句号切分。
    """
    flow = re.sub(r"=+第\d+页=+", "。", flat)
    flow = re.sub(r"山西兰花真诚招标代理有限公司[^。]{0,80}", "", flow)
    flow = re.sub(r"招标文件", "", flow)
    flow = re.sub(r"\n", "", flow)
    sentences: list[str] = []
    for raw in re.split(r"[。]", flow):
        text = raw.strip(" ；;，,")
        if len(text) < 10:
            continue
        if re.search(r"支付合同价款的?\s*\d{1,2}\s*[%％]", text) or re.search(r"质保期满|质保金", text):
            sentences.append(text)
    if not sentences:
        return "", []
    ratios: list[str] = []
    for text in sentences:
        for value in re.findall(r"(\d{1,2})\s*[%％]", text):
            if value not in ratios:
                ratios.append(value)
    joined = "；".join(sentences[:4])
    joined = joined.split("第六条")[0]
    return joined[:300], ratios


def _as_wan(amount: str) -> str:
    try:
        wan = float(amount) / 10000
    except (TypeError, ValueError):
        return ""
    return f"{wan:g}万元"


def _scaled(value: str, unit: str) -> str:
    """统一折算成「元」便于比较报价是否超预算。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{int(number * (10000 if unit == '万元' else 1))}"


def render_commercial_context(terms: dict[str, str] | None) -> str:
    """渲染成注入每章共享上下文的「商务承诺基线」。无字段时返回空串。"""
    if not terms:
        return ""
    lines: list[str] = []
    if terms.get("validity"):
        lines.append(f"· 投标有效期：自投标截止时间起 {terms['validity']}")
    if terms.get("bond"):
        lines.append(f"· 投标保证金：人民币 {terms['bond']}")
    if terms.get("budget"):
        lines.append(f"· 采购预算/最高限价：{terms['budget']}（投标报价不得超过，超过即否决）")
    if terms.get("duration"):
        lines.append(f"· 工期（交货期）：合同签订后 {terms['duration']}")
    if terms.get("warranty_months"):
        text = f"· 质保期：整体验收合格后起 {terms['warranty_months']} 个月"
        if terms.get("wear_months"):
            text += f"；易损件（高压胶管、密封件等）{terms['wear_months']} 个月"
        lines.append(text)
    if terms.get("place"):
        lines.append(f"· 交货地点：{terms['place']}")
    if terms.get("payment"):
        line = f"· 付款方式：{terms['payment']}"
        if terms.get("payment_ratios"):
            line += f"（原文比例：{terms['payment_ratios']}）"
        lines.append(line)
    if terms.get("owner_contact") or terms.get("owner_phone"):
        parts = []
        if terms.get("owner_contact"):
            parts.append(f"联系人 {terms['owner_contact']}")
        if terms.get("owner_phone"):
            parts.append(f"电话 {terms['owner_phone']}")
        if terms.get("owner_email"):
            parts.append(f"邮箱 {terms['owner_email']}")
        lines.append("· 招标人联系方式（原文给出）：" + "，".join(parts)
                     + "。正文直接引用，禁止写成待填；招标人现场负责人等原文未给的，写「由招标人指定」")
    if not lines:
        return ""
    return (
        "【商务承诺基线（招标原文口径，一票否决项）】\n"
        + "\n".join(lines)
        + "\n写作要求：凡涉及上述期限、金额与地点，必须与本基线完全一致；"
          "不确定时沿用基线表述，不得自行给出更短或更长的期限、更低的配置或额外的付款条件。"
          "不要把招标人自身的信息（联系人、电话、现场负责人）写成待填项——"
          "原文没有的就写「由招标人指定」或不写。"
    )


_NEGATIVE_WORDS = ("不得低于", "不低于", "不短于", "不少于", "至少")


def check_commercial_consistency(terms: dict[str, str] | None, body: str) -> list[str]:
    """正文写出与基线相反的商务口径时返回问题（供质量修订用）。"""
    if not terms or not body:
        return []
    flat = _flat(body)
    issues: list[str] = []

    validity = terms.get("validity") or ""
    expected_days = re.match(r"(\d+)", validity)
    if expected_days:
        for match in re.finditer(r"投标有效期[^。；\n]{0,30}?(\d{2,3})\s*(?:个)?(日历天|天)", flat):
            if match.group(1) != expected_days.group(1):
                issues.append(
                    f"投标有效期写成{match.group(1)}{match.group(2)}，"
                    f"招标文件要求自投标截止时间起{validity}（负偏离即废标）"
                )

    duration = terms.get("duration") or ""
    expected = re.match(r"(\d+)", duration)
    expected_unit = re.match(r"\d+(\D+)", duration)
    if expected:
        for match in re.finditer(
            r"(?:工期|交货期|服务期|供货期|合同签订后)[^。；\n]{0,20}?(\d+)\s*(?:个)?(日历天|天|工作日)", flat
        ):
            if match.group(1) != expected.group(1):
                issues.append(
                    f"{match.group(1)}{match.group(2)}与招标文件工期{duration}不一致"
                    "（付款方式与交货期负偏离作废标处理）"
                )
            elif expected_unit and match.group(2) != expected_unit.group(1):
                issues.append(f"工期单位写成{match.group(2)}，招标文件为{duration}，须按原口径响应")

    months = terms.get("warranty_months") or ""
    if months.isdigit():
        wear = terms.get("wear_months") or ""
        for match in re.finditer(r"质保期[^。；\n]{0,40}?(\d+)\s*个?月", flat):
            prefix = flat[max(0, match.start() - 12):match.start()]
            if re.search(r"易损件|胶管|密封件", prefix):
                continue
            if match.group(1) != months:
                issues.append(
                    f"质保期写成{match.group(1)}个月，招标文件为验收合格后起{months}个月（易损件{wear or '—'}个月）"
                )

    budget = terms.get("budget_value") or ""
    if budget.isdigit():
        limit = int(budget)
        for match in re.finditer(r"投标(?:总)?报价[^。；\n]{0,20}?([0-9]+(?:\.[0-9]+)?)\s*(万元|元)", flat):
            amount = float(match.group(1)) * (10000 if match.group(2) == "万元" else 1)
            if amount > limit:
                issues.append(
                    f"投标报价{match.group(1)}{match.group(2)}超过采购预算{terms.get('budget')}，超过即否决"
                )
    return list(dict.fromkeys(issues))