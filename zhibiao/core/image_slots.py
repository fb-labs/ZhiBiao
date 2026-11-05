"""插图占位框：语法、说明文案、识别与渲染口径的**唯一来源**。

背景（线上现状）
----------------
正文里「需要插图的地方」过去有三种互不统一的写法：

    【图片占位】此处插入相关架构图 / 流程图 / 现场照片，导出后可在 Word 中直接替换。
    此处需插入图片：营业执照
    {{图片占位:系统架构图}}          （生成器后来改用这种）

三种写法各有自己的正则：导出侧一套、章节状态一套、前端又一套（前端甚至不认
`{{图片占位:…}}`，正文里出现的占位框在预览区被当普通文字），说明文字还常常只写
「请插入图片」，出件人根本不知道要插什么、要不要盖章、要不要截全。本模块把语法与
文案收敛成一处：

统一标记（生成、存储、导出、前端识别都只认它）
--------------------------------------------
    {{图片占位:<从上下文推断出的具体插图要求>}}

规则
----
* 只写独立一行：行内出现的标记不画图框（会成为普通句子的一部分），导出时按红字提示处理。
* 说明文字必须回答三件事：**插什么**、**要不要盖章/签字**、**要不要截全**；
  不允许泛泛的「请插入图片」。`describe_image_kind()` 把「营业执照」「资质证书」这类
  短名补成「营业执照副本扫描件（加盖投标人公章；四角完整、统一社会信用代码清晰可辨）」。
* 旧写法（`【图片占位】…` / `此处需插入图片…`）只保留识别与迁移，不再生成；
  `normalize_image_placeholders()` 在生成落库前把它们改成统一标记。

消费方
------
* 生成侧：`image_placeholder()` / `normalize_image_placeholders()`
  （`app/core/chapter_rules.py`、`app/core/generation_runner.py`）；
* 导出侧：`IMAGE_SLOT_LINE` / `image_slot_hint()` / `slot_lines()`
  （`app/pipeline_api.py` 渲染成 1×1 带框表格 + 红色说明文字）；
* 前端：`frontend/src/imageSlots.ts` 与这里逐字对应（同一份语法的 TS 镜像），
  预览区渲染成浅红底 + 红字 + 虚线边框的预留图框。
"""

from __future__ import annotations

import re

# ── 语法（唯一来源）──────────────────────────────────────────────────────────

#: 标记前缀。导出/前端/检查都从这里取，不再各写一套正则。
IMAGE_PLACEHOLDER_TAG = "图片占位"
IMAGE_PLACEHOLDER_PREFIX = "{{%s:" % IMAGE_PLACEHOLDER_TAG

#: 标记本体。说明里不允许出现换行与右花括号（保证只占一行、可正则识别）。
IMAGE_PLACEHOLDER_RE = re.compile(r"\{\{图片占位[:：]([^}\r\n]*)\}\}")

#: 旧写法：只识别、只迁移，不再生成。
LEGACY_IMAGE_PREFIXES: tuple[str, ...] = ("【图片占位】", "此处需插入图片")

#: 独立成行的占位标记（含两种旧写法）。`^...$` + re.MULTILINE：逐行判定。
#: 注意：行内出现（前后还有别的字）不算占位框，不画框。
IMAGE_SLOT_LINE = re.compile(
    r"^[ \t]*(?:"
    r"\{\{图片占位[:：][^}\r\n]*\}\}"
    r"|【图片占位】[^\r\n]*"
    r"|此处需插入图片[^\r\n]*"
    r")[ \t]*$",
    re.MULTILINE,
)

# 旧写法整行（迁移用，必须整行匹配，避免把正文里「此处需插入图片的情况」这类描述改掉）。
_LEGACY_LINE_FULL = re.compile(
    r"^[ \t]*(?:【图片占位】[^\r\n]*|此处需插入图片[^\r\n]*)[ \t]*$")


# ── 说明文案：插什么 + 要不要盖章 + 要不要截全 ───────────────────────────────

#: (命中关键词, 具体插图要求)。顺序即优先级：更专的品类放前面
#: （「矿用产品安全标志证书」必须先于「资质证书」，「体系认证证书」先于「认证证书」）。
IMAGE_KINDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("营业执照", "执照"),
     "营业执照副本扫描件（加盖投标人公章；正本或副本均可，四角完整、"
     "统一社会信用代码与经营范围清晰可辨）"),
    (("开户许可证", "基本账户", "基本存款", "开户行", "银行账户"),
     "开户许可证或基本存款账户信息扫描件（加盖投标人公章；开户行名称、账号与"
     "银行业务章清晰可辨）"),
    (("法定代表人身份证", "法人身份证", "身份证", "身份证明"),
     "法定代表人身份证正反面扫描件（正反面同页排布；姓名、身份证号码与签发机关"
     "清晰可辨，不得遮挡号码）"),
    (("授权委托书", "委托书"),
     "授权委托书签字盖章后的扫描件（法定代表人签字 + 加盖单位公章；整页四角完整，"
     "委托人、受托人与授权事项不得裁切）"),
    (("矿用产品安全标志", "安全标志证书", "煤安", "MA标志"),
     "矿用产品安全标志证书（MA 标志）扫描件（含证书编号、有效期、发证机构与"
     "获证产品型号页，加盖投标人公章）"),
    (("体系认证", "ISO", "3C认证", "认证证书"),
     "有效期内的体系认证证书扫描件（含证书编号、认证范围与有效期页，加盖投标人公章）"),
    (("资质证书", "资质", "许可证", "许可证书"),
     "资质证书扫描件（覆盖本项目要求的资质等级与业务范围；含证书编号、有效期与"
     "发证机关页，加盖投标人公章）"),
    (("业绩合同", "合同关键页", "类似项目合同", "供货合同"),
     "业绩合同关键页扫描件（首页 + 盖章签署页；能看清项目名称、供货内容、合同金额"
     "与双方盖章，中间条款页可只截目录页）"),
    (("信誉查询", "信用中国", "企业信用", "执行信息公开", "失信", "网页截图", "查询截图"),
     "信誉查询网页截图（{name}；须截全网址、查询日期与查询结果，不得只截结论区域，"
     "并加盖投标人公章）"),
    (("检验报告", "检测报告", "试验报告", "报告首页"),
     "检验/检测报告首页扫描件（含报告编号、检验结论、检测机构盖章页；整页完整，"
     "不得只截结论一行）"),
    (("财务报表", "审计报告", "资产负债表"),
     "财务报表或审计报告关键页扫描件（含报表封面、资产负债表与审计机构盖章页，"
     "加盖投标人公章）"),
    (("银行资信", "资信证明"),
     "银行资信证明扫描件（开户行出具并加盖银行业务章，含出具日期与授信/存款结论）"),
    (("纳税", "社保", "完税"),
     "纳税或社保缴纳凭证扫描件（含所属期、征收机关与金额，加盖投标人公章）"),
    (("保证金", "保函", "缴纳凭证", "转账凭证"),
     "投标保证金缴纳凭证或银行保函扫描件（含付款账号、金额、到账日期与银行盖章）"),
    # 示意图/流程图放在实物照片**之前**：旧标记「相关架构图 / 流程图 / 现场照片」
    # 是一句话写多个图位，按「架构图」口径补要求更能覆盖这一整行。
    (("架构图", "流程图", "拓扑图", "示意图", "时序图", "界面"),
     "{name}（整图完整截取，图内文字清晰可辨；软件界面截图须截全窗口，"
     "不得裁掉菜单栏与状态栏）"),
    (("设备实物", "实物照片", "设备照片", "现场照片", "铭牌"),
     "设备实物照片（整机全景 + 铭牌特写各一张；光线充足，铭牌上的型号、参数与"
     "出厂编号清晰可辨）"),
)

#: 品类都命中不上时的兜底：仍然写清「插什么」与截图/盖章口径，
#: 不允许退回「请插入图片」这种废话。
FALLBACK_TEMPLATE = (
    "{name}扫描件（整页完整、关键信息与盖章处清晰可辨；如为网页或系统截图，"
    "须截全网址与查询条件）"
)

#: 连图名都没给时的最后兜底（只会在数据异常时出现）。
DEFAULT_REQUIREMENT = (
    "与本段内容对应的证明材料扫描件（整页完整、关键信息与盖章处清晰可辨；"
    "如为网页或系统截图，须截全网址与查询条件）"
)

#: 图框里显示的两行固定文案（导出、前端共用同一口径）。
BOX_TITLE = "[待插入图片]"
BOX_SIZE_TIP = "尺寸建议：宽 15cm 以内，需清晰可辨"


def _clean_name(kind: str) -> str:
    """去掉调用方可能带进来的标记/旧前缀/多余空白，只留图名本体。"""
    name = str(kind or "").strip()
    names = []
    for line in name.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(IMAGE_PLACEHOLDER_PREFIX) or line.startswith("{{图片占位："):
            line = line.split(":", 1)[-1] if ":" in line else line
            line = line.split("：", 1)[-1]
        for prefix in LEGACY_IMAGE_PREFIXES:
            if line.startswith(prefix):
                line = line[len(prefix):]
                break
        # 旧说明里的操作提示不是图名：「，导出后可在 Word 中直接替换。」
        line = re.sub(r"[，,；;]?\s*导出后可在\s*Word\s*中直接替换[。.]?", "", line)
        line = re.sub(r"^此处(?:需)?插入(?:图片)?[：:，,\s]*", "", line)
        line = line.strip().strip("}").strip()
        line = line.strip("。.").strip()
        line = line.lstrip("：: ").strip()
        if line:
            names.append(line)
    return " ".join(names).strip()


def describe_image_kind(kind: str) -> str:
    """把图名/短标记补成「插什么 + 要不要盖章 + 要不要截全」的具体要求。

    命中品类 → 用该品类的固定口径（出件人一眼知道要交哪种扫描件）；
    已经是写好的长说明 → 原样保留（只补齐「截全」口径）；
    其它 → 用图名拼出具体扫描件要求，绝不返回泛泛的「请插入图片」。
    """
    name = _clean_name(kind)
    if not name:
        return DEFAULT_REQUIREMENT
    folded = name.casefold()
    # 命中即用：词库按「专有品类优先」排序（矿用安标 > 资质，开户许可 > 许可证…），
    # 因此第一个命中的就是最贴近这一行的口径。
    for keywords, requirement in IMAGE_KINDS:
        if any(keyword.casefold() in folded for keyword in keywords):
            filled = requirement.replace("{name}", name)
            # 幂等：图名已经是「同一口径 + 自带括号说明」的成品（重复归一、渠道图框二次
            # 归一都会遇到），原样返回，否则每归一一次就多套一层括号。
            if name.startswith(filled.split("（", 1)[0]) and "（" in name:
                return name
            return filled
    if len(name) >= 24 or re.search(r"(扫描件|复印件|截图|照片|图片)", name):
        if name.endswith(("）", ")", "。")):
            return name
        return name + "（整页完整、关键信息与盖章处清晰可辨；截图须截全网址与查询条件）"
    return FALLBACK_TEMPLATE.replace("{name}", name)


def image_placeholder(kind: str) -> str:
    """生成统一插图占位标记（生成侧唯一写法，导出/前端复用同一语法）。"""
    return "%s%s}}" % (IMAGE_PLACEHOLDER_PREFIX, describe_image_kind(kind))


def image_slot_hint(line: str) -> str:
    """从一行占位标记/旧写法/裸图名里取出**具体插图要求**（导出与前端都用它）。"""
    text = str(line or "").strip()
    matched = IMAGE_PLACEHOLDER_RE.search(text)
    if matched:
        return describe_image_kind(matched.group(1))
    for prefix in LEGACY_IMAGE_PREFIXES:
        if text.startswith(prefix):
            return describe_image_kind(text[len(prefix):])
    return describe_image_kind(text)


def slot_lines(hint: str) -> tuple[str, str]:
    """图框里要显示的两行文字：(标题+具体要求, 尺寸建议)。"""
    return "%s 此处插入：%s" % (BOX_TITLE, hint), BOX_SIZE_TIP


def is_image_slot_line(line: str) -> bool:
    """这一整行是不是插图占位框（生成/检查/导出/状态判断都走这里）。"""
    return bool(IMAGE_SLOT_LINE.fullmatch(str(line or "")))


def has_image_slot(text: str | None) -> bool:
    """正文里是否有独立成行的插图占位框。"""
    return bool(IMAGE_SLOT_LINE.search(str(text or "")))


def normalize_image_placeholders(text: str) -> tuple[str, int]:
    """把旧写法与短图名归一成统一标记。返回 (新文本, 改动行数)。

    * `【图片占位】说明` / `此处需插入图片：说明`（整行）→ `{{图片占位:<具体说明>}}`；
    * `{{图片占位:营业执照}}`（短名）→ `{{图片占位:营业执照副本扫描件（加盖…）}}`；
    * 行内的标记与普通文字不动，避免把正文描述改坏。
    """
    body = str(text or "")
    if not body:
        return body, 0
    changes = 0
    out: list[str] = []
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped):]
        if _LEGACY_LINE_FULL.fullmatch(stripped):
            out.append(image_placeholder(stripped) + ending)
            changes += 1
            continue
        matched = IMAGE_PLACEHOLDER_RE.fullmatch(stripped.strip())
        if matched:
            wanted = describe_image_kind(matched.group(1))
            if wanted != matched.group(1):
                indent = stripped[: len(stripped) - len(stripped.lstrip())]
                out.append(indent + image_placeholder(wanted) + ending)
                changes += 1
                continue
        out.append(line)
    return "".join(out), changes