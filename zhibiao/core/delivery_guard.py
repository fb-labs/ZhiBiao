"""交付前污染自检（护栏 B）：导出前确认正文没有混入别的项目/别的标的物的内容。

为什么需要这一层：大纲域校验（护栏 E）只能拦住**新建**大纲；已经落库的旧项目
（例如线上「新3」，83 章里 5 章是液压支架骨架）如果直接点导出，错误的章节名会原样
进 Word。这一层在导出前做最后一道体检，把两类污染直接摆到人面前：

1. **其他项目的专有名词**：正文里出现只存在于别的项目采购文件里的公司/矿名
   （线上审计到的现象：减速器标书的正文里出现「阳泉煤业」「新景矿」）。
   白名单取本项目**全部**上传文件里的专有名词，所以本项目自己的招标人、
   代理机构、分包名称绝不会被误报。
2. **跨域章节骨架**：章节标题含本项目标的物不可能有的专属词
   （减速器项目里的「架型 / 电液控制 / 支架压力监测 / 井下支护」）。
   与大纲剔除共用 app/core/subject.py 的同一张专属词表。

一律只提示：`POST /projects/{id}/export` 不再因为本自检返回 409，
结论降级成响应头 `X-Export-Warnings` 里的 `kind="跨项目数据"`（导出照常出件）。
命中说明是启发式的，`detail` 会点名命中的词，供人工一眼判断。
"""
# ── 规则来源约定（用户 2026-10-19 明确要求）──────────────────────────────────
# 上面引用的是**示例项目**的现象记录，**不是规则本身**。本项目上传件都是例子，
# 所以本模块只允许依赖：
#   1) 通用领域知识（中文机构名后缀、矿名写法、上下文虚词…对所有项目一视同仁）；
#   2) 运行时从数据库里抽出来的真实数据（本项目全部文件 = 白名单；
#      其它项目的文件 = 用来发现外来专名的对照样本）。
# 严禁把某个示例项目的公司名/矿名/项目名写进表里或做特判；
# 回归见 tests/test_no_example_data_in_logic.py。
from __future__ import annotations

import re
from typing import Any, Iterable

from zhibiao.core.subject import SUBTYPE_NAMES, foreign_domain_terms

# 单位名后缀：中文机构名的可靠锚点（名字可能很长，但一定以这些词结尾）
COMPANY_SUFFIX = re.compile(r"有限责任公司|股份有限公司|有限公司|集团公司|集团"
                            r"|煤业|煤矿|矿业|物资经销公司|管理处|管理局")
MINE_CHAR = "矿"
# 「矿」后面跟这些字说明不是名字（矿用/矿山/矿井…）
MINE_SKIP_NEXT = set("用山井区方长灯业产")
# 「矿」前面紧贴这些字时也不是名字，而是普通名词/动词短语：
# 「设备到矿」「货物运至矿」「材料进场」「人员入矿」——「矿」只是目的地。
# 不排除的话会被抽成专有名词「设备到矿」，进而在跨项目污染护栏里误报，
# 实测把整本标书的导出拦成 409（出现其他项目的专有名词：设备到矿）。
MINE_VERB_CHARS = set("到至入进运送达抵装卸发回上下离出赴临于用在和与及跟向从对为把被由经自于"
                      "有实效具现存含属称")
# 「矿」前那个字是上面这些时已能排除「设备到矿」一类；但散文里还有「具有矿」「落实矿」
# 「有效的煤矿」这种框进两三个字的情况（单字判断看不出来）。整词再兜一层。
# 实测这些碎片会让别的项目被误判成「出现其他项目的专有名词」，把整本导出拦成 409。
MINE_VERB_WORDS = (
    "具有", "落实", "有效", "存在", "采用", "使用", "运至", "发往", "送至", "进入",
    "位于", "属于", "符合", "满足", "包括", "涉及", "覆盖", "到达", "到达", "符合要",
)
# 名字前面常见的上下文词：向左回扫会把它们一起框进来，必须迭代剥掉。
# 注意**不能**用「单字作边界」的做法 —— 「物资经销有限责任公司」里的「经」会被误当成虚词，
# 名字就被切成「销有限责任公司」了。这里按整词剥离，且剥完必须还剩 >= 4 个字。
CONTEXT_PREFIXES = (
    "本项目由", "本项目", "项目由", "采购代理机构", "代理机构", "中标供应商", "中标人",
    "采购人", "招标人", "投标人", "供应商", "纳税人名称", "开户名", "收款单位", "收款人",
    "账户名", "监造单位", "使用单位", "需求单位", "项目单位", "建设单位", "承建单位",
    "联系电话", "联系人", "地址", "地点", "电话", "名称", "全称", "单位", "业主",
    "发包人", "承办人", "甲乙双方", "甲方", "乙方",
    # 常见散文动词/介词：右侧锚定后靠它们把「监督部门为××公司」这类碎片切干净
    "适用于", "满足", "使用", "采用", "遵守", "关于", "位于", "依据", "按照", "根据",
    "符合", "执行", "参照", "要求", "监督", "属于", "归属",
    "由", "是", "在", "与", "受", "向", "对", "被", "于", "该", "我", "贵", "其", "此",
    "这", "那", "的", "了", "将", "把", "为", "和", "及", "到", "需", "须", "应",
)
MAX_NAME_LEN = 18
# 名字总长上限：超过它说明左边的散文被框进来了，从左截断（截出来的仍是真名字的子串，
# 正文里出现全称时依然能命中）。
MAX_NAME_TOTAL = 20

# 不是专有名词的通用说法，出现频率高，误报代价大
STOP_NAMES = {
    "煤矿", "本矿", "该矿", "我矿", "各矿", "全矿", "矿井", "矿山", "矿用", "矿区",
    "尾矿", "选矿", "露天矿", "矿方", "甲方", "乙方", "本公司", "贵公司", "该公司",
    "有限公司", "有限责任公司", "集团", "煤业", "矿业", "采购人", "投标人", "供应商",
}
# 矿名里混进这些词说明切错了（例如从「…物资经销有限责任公司新景矿」里切出来的碎片）
BAD_NAME_PARTS = ("公司", "集团", "有限责任", "管理", "经销", "采购", "标段")
MAX_OTHER_FILES = 40          # 每个项目最多扫 40 份别的文件
SCAN_WINDOW = 60000           # 每份文件只扫前 6 万字符（采购人/项目名都在开头）
MAX_TERMS = 80                # 最多报 80 个外来专有名词，避免提示刷屏


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _walk_left(text: str, anchor: int) -> int:
    """从 anchor 向左回扫名字，返回起点下标（遇到非汉字或超长就停）。"""
    pos = anchor - 1
    while pos >= 0 and _is_cjk(text[pos]) and (anchor - pos) <= MAX_NAME_LEN:
        pos -= 1
    return pos + 1


def _trim_context_prefix(name: str) -> str:
    """剥掉名字前面的上下文词。

    取**最靠右**的那个上下文词之后再算名字：「本技术要求适用于阳煤集团寿阳景福煤业有限公司」
    里最右的虚词是「于」，截出来就是干净的「阳煤集团寿阳景福煤业有限公司」。
    单字虚词（为/和/及/到…）只在下标 > 0 时才截 —— 否则「为民机械有限公司」「和平矿业有限公司」
    这种以虚词开头的真实名字会被切掉第一个字。截出来的片段仍是真名字的**后缀**，
    所以正文里出现全称时照样能命中，最多是提示里显示得短一些。
    """
    cut = 0
    for prefix in CONTEXT_PREFIXES:
        index = name.rfind(prefix)
        if index < 0 or (len(prefix) == 1 and index == 0):
            continue
        end = index + len(prefix)
        if end > cut and len(name) - end >= 4:
            cut = end
    return name[cut:]


def _normalize_name(name: str) -> str:
    name = _trim_context_prefix(name)
    if len(name) > MAX_NAME_TOTAL:
        name = name[-MAX_NAME_TOTAL:]
    return name


def _company_spans(whole: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in COMPANY_SUFFIX.finditer(whole):
        start = _walk_left(whole, match.start())
        name = _normalize_name(whole[start:match.end()].strip())
        # 「煤矿」本身就在 COMPANY_SUFFIX 里，散文片段「有效的煤矿」会被当成机构名；
        # 助词「的」与动词短语不可能是名字的一部分，先排掉。
        if "的" in name or any(word in name for word in MINE_VERB_WORDS):
            continue
        if len(name) >= 4 and name not in STOP_NAMES:
            found = whole.find(name, start, match.end())
            spans.append((found if found >= 0 else start, match.end(), name))
    return spans


def proper_nouns(text: Any) -> set[str]:
    """抽出一段文本里的公司名/矿名（专有名词）。

    * 公司名：锚定「有限责任公司/有限公司/集团/煤业…」等后缀，再向左回扫到虚词为止。
      不回扫的话「…由阳泉煤业集团物资经销有限责任公司组织」会整段变成名字。
    * 矿名：把已识别的公司名抠掉再找「矿」，否则「…经销有限责任公司新景矿」会被切成碎片；
      同时收录全称与末尾简称（「阳泉市新景矿」→ 也收录「新景矿」），两种写法都要能命中。
    """
    whole = str(text if text is not None else "")
    if not whole:
        return set()
    found: set[str] = set()
    company_spans = _company_spans(whole)
    for _start, _end, name in company_spans:
        found.add(name)
    # 抠掉公司名后再找矿名
    residual = list(whole)
    for start, end, _name in company_spans:
        for index in range(start, end):
            residual[index] = " "
    residual_text = "".join(residual)
    for match in re.finditer(MINE_CHAR, residual_text):
        end = match.end()
        if end < len(residual_text) and residual_text[end] in MINE_SKIP_NEXT:
            continue
        # 「设备到矿」「运至矿方」：矿前是动词/介词，这是普通名词不是矿名
        if match.start() > 0 and residual_text[match.start() - 1] in MINE_VERB_CHARS:
            continue
        start = _walk_left(residual_text, match.start())
        name = _normalize_name(residual_text[start:end].strip())
        # 万一回扫跨过了动词（「…设备到矿」整段被框住），末尾仍以动词收尾则丢弃
        _stem = name[:-1] if name.endswith(MINE_CHAR) else name
        if _stem and _stem[-1] in MINE_VERB_CHARS:
            continue
        # 「有效的煤矿」「该矿的运输」这类片段：助词「的」不可能是矿名的一部分
        if "的" in name:
            continue
        if any(word in name for word in MINE_VERB_WORDS):
            continue
        candidates = [name]
        tail = name[-3:]
        if len(name) > 3 and tail.endswith(MINE_CHAR):
            candidates.append(tail)
        for candidate in candidates:
            if (len(candidate) >= 3 and candidate not in STOP_NAMES
                    and not any(part in candidate for part in BAD_NAME_PARTS)):
                found.add(candidate)
    return found


def _covered_by_own(candidate: str, own: set[str]) -> bool:
    """候选外来名字是否其实已经被本项目自己的某个名字覆盖（防误报的后手）。

    只做两件很保守的判断：一个是另一个的子串；或两者共享 >= 8 个字且 >= 10 个字的机构名尾部。
    「有限责任公司」这种 6 字通用尾巴故意不算 —— 那会把完全不同的两家公司判成同一家。
    """
    for name in own:
        if len(name) >= 6 and (name in candidate or candidate in name):
            return True
        if min(len(name), len(candidate)) >= 10 and name[-8:] == candidate[-8:]:
            return True
    return False


def collect_foreign_terms(own_texts: Iterable[Any], other_texts: Iterable[Any]) -> list[str]:
    """本项目不该出现的专有名词 = 别的项目文件里有、而本项目所有文件里都没有的名字。

    ``own_texts`` 必须包含**本项目全部**上传文件，否则同项目第二份文件里出现过的名字
    会被误判成外来污染。
    """
    own: set[str] = set()
    for text in own_texts:
        own |= proper_nouns(str(text or "")[:SCAN_WINDOW])
    foreign: set[str] = set()
    for text in other_texts:
        for name in proper_nouns(str(text or "")[:SCAN_WINDOW]):
            if name not in own and not _covered_by_own(name, own):
                foreign.add(name)
    # 长的名字更具体（「阳泉市新景矿」优先于「新景矿」），前面的提示更有信息量
    return sorted(foreign, key=lambda name: (-len(name), name))[:MAX_TERMS]


def _drop_subsumed(names: list[str]) -> list[str]:
    """同一家单位的多个写法只留最长的那个，避免提示里重复报「××集团、××集团物资…」。

    只在**显示**时去重：匹配用的集合保留全部写法（正文里可能只写了简称）。
    """
    out: list[str] = []
    for name in names:
        if any(other != name and name in other for other in names):
            continue
        out.append(name)
    return out


def pollution_findings(
    chapters: Iterable[dict[str, Any]],
    foreign_terms: Iterable[str],
    subject_meta: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """逐章体检，返回 [{seq, title, issues:[中文说明]}]（空列表 = 干净）。"""
    terms = [str(t) for t in (foreign_terms or []) if str(t or "").strip()]
    domain_terms = foreign_domain_terms(subject_meta or {})
    findings: list[dict[str, Any]] = []
    for chapter in chapters:
        title = str(chapter.get("title") or "")
        content = str(chapter.get("content") or "")
        issues: list[str] = []
        if terms:
            hits = _drop_subsumed([t for t in terms if t in title or t in content])
            if hits:
                shown = "、".join(hits[:5])
                suffix = f" 等 {len(hits)} 个" if len(hits) > 5 else ""
                issues.append(f"出现其他项目的专有名词：{shown}{suffix}")
        # 正文里提一句别的设备很正常，只有**标题**串了别的标的物骨架才算事故
        domain_hits = [w for w in domain_terms if w in title]
        if domain_hits:
            subtype = str((subject_meta or {}).get("subtype") or "")
            own_name = SUBTYPE_NAMES.get(subtype, "本项目标的物")
            issues.append(f"章节骨架与「{own_name}」不符（含专属词「{domain_hits[0]}」）")
        if issues:
            findings.append({"seq": chapter.get("seq"), "title": title, "issues": issues})
    return findings


def pollution_message(findings: list[dict[str, Any]], limit: int = 8) -> str:
    """拼成给用户看的一句话（前端已有的「仍然导出」确认框会原样显示）。"""
    examples = "；".join(
        f"第 {item.get('seq')} 节《{item.get('title')}》" + ("（" + "，".join(item["issues"]) + "）")
        for item in findings[:limit])
    suffix = f"；另有 {len(findings) - limit} 节" if len(findings) > limit else ""
    return ("导出前污染自检未通过：正文里疑似混入了其他项目或其他标的物的内容。"
            + examples + suffix
            + "。建议先核对招标原文并重新生成大纲、重跑相关章节；确认无误也可以明确选择仍然导出。")