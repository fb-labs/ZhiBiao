"""Structured, source-grounded extraction for the 15 tender interpretation dimensions."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from zhibiao.core.source_excerpt import balanced_excerpt
from zhibiao.core.llm_status import llm_ready


DETAIL_DIMENSIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "project_info", "name": "项目信息",
        "keywords": ("项目名称", "项目编号", "招标编号", "采购编号", "采购方式", "预算金额", "最高限价", "项目概况"),
        "schema": {
            "project_name": "项目名称", "project_code": "项目编号/招标编号",
            "procurement_method": "采购方式", "budget_amount": "预算金额或最高限价（保留币种、大小写及口径）",
            "procurement_unit": "采购人/招标人名称", "project_overview": "项目范围、标的、数量和目标的原文概述",
        },
        "instruction": "项目范围、标的和数量尽量完整摘录，区分预算、最高限价和单项限价。",
    },
    {
        "id": "buyer_info", "name": "甲方信息",
        "keywords": ("采购人", "招标人", "采购单位", "招标单位", "甲方", "单位地址", "邮政编码", "主管部门"),
        "schema": {
            "unit_name": "采购单位/招标人全称", "address": "单位地址", "contact_person": "甲方联系人",
            "contact_phone": "甲方联系电话", "supervisor_dept": "主管部门",
        },
        "instruction": "没有明确写出的联系人、地址或电话填 null，不从其他主体的信息推断。",
    },
    {
        "id": "qualification", "name": "资格要求",
        "keywords": ("资格条件", "资格要求", "投标人资格", "资质要求", "营业执照", "业绩要求", "项目负责人", "联合体"),
        "schema": {
            "qualification_level": "资质类别、等级及证书要求", "registered_capital": "注册资本要求",
            "performance_requirement": "业绩数量、时间范围、金额和证明材料要求",
            "personnel_requirement": "项目经理及其他人员的资格、社保、证书要求",
            "equipment_requirement": "设备或资源要求", "other_requirements": "信用、财务、联合体、分包等其他资格条件",
        },
        "instruction": "按每项资格条件分别列出，并保留年限、金额、数量、证书和证明材料要求。",
    },
    {
        "id": "technical", "name": "技术需求",
        "keywords": ("技术参数", "技术要求", "采购需求", "技术规格", "服务要求", "验收标准", "★", "▲", "实质性参数"),
        "schema": {
            "technical_parameters": "主要技术参数、规格、数量及逐项指标（列表）",
            "technical_standards": "引用的技术标准、规范及版本", "service_requirements": "服务范围、响应时限、售后和培训要求",
            "acceptance_criteria": "验收条件、流程、标准及交付物", "mandatory_params": "★、▲或明确标为实质性/强制性的参数（列表）",
            "supply_inventory": "按采购对象保留主机、配套件、备件及工具的完整行：名称、型号、数量、单位、备注和出处，不以摘要替代",
            "training_requirements": "培训对象、人数/人日/课时及各自单位、免费范围、考核、效果确认要求",
            "integration_requirements": "接入对象、接口、兼容要求、费用承担及验证条件",
            "test_requirements": "出厂和运行现场试验项目、测试方法、装备、判据与依据；未给出的判据不得补造",
        },
        "instruction": "尽量逐项保留指标、单位、允许偏差和★/▲标记；不要把多项参数合并成笼统总结。",
    },
    {
        "id": "scoring", "name": "评分细则",
        "keywords": ("评分办法", "评分标准", "评分细则", "评审因素", "评分因素", "分值", "价格分", "技术分", "商务分"),
        "schema": {
            "evaluation_method": "评标方法", "business_weight": "商务分总分/权重",
            "technical_weight": "技术分总分/权重", "price_weight": "价格分总分/权重",
            "scoring_items": "每个具体评分项的名称、分值、完整评分档次/扣分标准（列表）",
        },
        "instruction": "保留所有具体评分项，不只列商务/技术/价格部分总分；分值合计不自行修正，原文未明确则说明。",
    },
    {
        "id": "disqualification", "name": "废标红线",
        "keywords": ("废标", "无效投标", "否决投标", "否决响应", "实质性要求", "不予受理", "视为无效"),
        "schema": {
            "substantive_requirements": "实质性响应要求（列表）", "mandatory_conditions": "强制性条件（列表）",
            "disqualification_clauses": "可能导致拒收、无效或否决的条款，逐条保留条款号和触发条件（列表）",
        },
        "instruction": "只摘取文件明确规定的触发条件及条款号，不扩大解释法律后果。",
    },
    {
        "id": "deposit", "name": "保证金",
        "keywords": ("投标保证金", "投标担保", "保证金金额", "保函", "保证金退还", "到账时间"),
        "schema": {
            "amount": "投标保证金金额（含大小写、币种）", "payment_method": "缴纳形式、账户或保函要求",
            "deadline": "缴纳截止时间及到账要求", "refund_conditions": "退还时间、条件和不予退还情形",
        },
        "instruction": "区分投标保证金、履约保证金和其他担保；账户信息完整摘录，严禁改写金额。",
    },
    {
        "id": "opening", "name": "开标要求",
        "keywords": ("开标时间", "开标地点", "开标大厅", "投标截止", "递交方式", "密封", "电子投标", "上传截止"),
        "schema": {
            "opening_time": "开标时间", "opening_location": "开标地点/线上开标入口",
            "sealing_requirements": "纸质文件密封、标识、份数要求", "submission_method": "线上/线下递交方式及操作要求",
            "submission_deadline": "投标文件递交截止时间及平台要求",
        },
        "instruction": "区分投标截止、开标时间和保证金到账时间，保留时区、平台、份数及密封要求。",
    },
    {
        "id": "evaluation", "name": "评标办法",
        "keywords": ("评标办法", "评审办法", "评标委员会", "评审委员会", "评标程序", "初步评审", "详细评审", "定标"),
        "schema": {
            "method": "评标/评审方法", "committee_composition": "评标委员会构成及人数（如有）",
            "evaluation_process": "资格审查、符合性审查、详细评审、排序/定标等流程步骤（列表）",
        },
        "instruction": "准确区分评标流程与投标人的响应义务；委员会信息缺失时填 null。",
    },
    {
        "id": "commercial", "name": "商务评分",
        "keywords": ("商务评分", "商务部分", "企业业绩", "企业资质", "财务状况", "信誉", "商务评审"),
        "schema": {
            "qualification_score": "企业资质/认证类商务评分标准", "performance_score": "企业业绩评分标准",
            "financial_score": "财务状况评分标准", "reputation_score": "信誉、信用或获奖评分标准",
            "other_items": "其他商务评分项、分值及评分档次（列表）",
        },
        "instruction": "按每个评分项分别保留分值、年限/数量门槛、证明材料和档次，不推断企业自身得分。",
    },
    {
        "id": "contract", "name": "合同条款",
        "keywords": ("合同条款", "付款方式", "支付方式", "违约责任", "违约金", "质保期", "争议解决", "履约期限", "交付成果", "开发成果", "科技成果", "知识产权", "专利", "论文", "项目报告", "成果验收"),
        "schema": {
            "payment_terms": "付款节点、比例、条件及发票要求", "breach_liability": "违约责任、违约金比例/上限及触发条件",
            "warranty_period": "质保/服务期限及起算点", "acceptance_standard": "合同验收标准和程序",
            "dispute_resolution": "争议解决方式、管辖地及适用条款",
            "deliverables": "合同及附件要求交付的全部设备、服务、文档与研发成果；逐项保留名称、数量、单位、期限、主体和验收条件",
            "intellectual_property": "成果权属、专利申请/授权、论文署名、费用承担、保密及使用许可；区分未来义务与已有资质成果",
            "milestone_dependencies": "付款、交付、试运行、分级验收及质保起算的依赖关系；相互不明或冲突时原样保留并标待核实",
        },
        "instruction": "保留比例、金额、工作日/自然日、起算条件和双方责任主体。",
    },
    {
        "id": "risk", "name": "风险提示",
        "keywords": ("风险", "排他", "唯一授权", "倾向性", "不合理", "单一来源", "违约", "投诉", "质疑"),
        "schema": {
            "exclusivity_clauses": "可能具有排他性的条款及原文依据（如有）",
            "biased_scoring": "需要关注的评分设置及原文依据（如有）",
            "unreasonable_requirements": "需要进一步核验合理性的要求及原文依据（如有）",
            "potential_risks": "执行、履约、递交或条款风险，写明依据和需人工核实事项（列表）",
        },
        "instruction": "风险只能标为待核查线索，不下违法结论；每项注明依据，无法确认时填 null。",
    },
    {
        "id": "competition", "name": "竞争态势",
        "keywords": ("竞争", "市场", "品牌", "型号", "兼容", "原厂", "授权", "业绩门槛", "专利"),
        "schema": {
            "potential_competitors": "由资质、品牌、授权、业绩门槛推导的竞争者类型，不列未经证实的企业名称",
            "market_pattern": "招标文件能支持的市场/准入特征；区分文件事实与判断",
            "competitive_advantages": "投标人可准备证明的响应优势方向；明确这是建议而非已具备事实",
        },
        "instruction": "不得从文件臆造具体竞争对手、市场份额或我方资质；把文件事实与分析建议分开标注。",
    },
    {
        "id": "timeline", "name": "时间节点",
        "keywords": ("公告日期", "公告时间", "答疑", "澄清", "质疑截止", "投标截止", "开标日期", "合同签订"),
        "schema": {
            "announcement_date": "公告发布日期", "clarification_deadline": "答疑/澄清/质疑截止时间",
            "bid_deadline": "投标/响应文件截止时间", "opening_date": "开标时间",
            "contract_signing_date": "合同签订期限/触发条件",
            "performance_milestones": "设计、供货、安装、培训、试运行、成果交付、分级验收等节点的起算事件、时长、单位及前置条件",
            "timeline_ambiguities": "不同条款工期、试运行及验收边界的不明或冲突之处，附各自原文，不自行压缩试验期或延长合同期限",
        },
        "instruction": "按时间先后整理并保留年份、时分、期限单位和起算事件；不得自行推算未明确日期。",
    },
    {
        "id": "contacts", "name": "关键联系人",
        "keywords": ("采购人联系人", "招标人联系人", "采购代理", "代理机构联系人", "技术联系人", "项目联系人", "联系电话", "电子邮箱"),
        "schema": {
            "buyer_contact_person": "采购人联系人、电话、邮箱", "buyer_contact_info": "采购人地址/联系方式",
            "agency_contact_person": "招标代理机构联系人、电话、邮箱", "agency_contact_info": "代理机构地址/联系方式",
            "technical_contact_person": "技术联系人", "technical_contact_info": "技术联系人电话/邮箱",
        },
        "instruction": "分清采购人、代理机构和技术联系人；姓名、电话、邮箱需原文出现才填写。",
    },
)


def _relevant_excerpt(text: str, keywords: tuple[str, ...], limit: int = 18000) -> str:
    """Retrieve source-ordered evidence from all regions; disclose any omissions."""
    return balanced_excerpt(text, keywords, limit)


def _clean_value(value: Any, depth: int = 0) -> Any:
    if depth > 4 or value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        cleaned = re.sub(r"\s+", " ", value).strip()
        return cleaned[:18000] if cleaned else None
    if isinstance(value, list):
        return [_clean_value(item, depth + 1) for item in value[:500]]
    if isinstance(value, dict):
        return {str(key)[:80]: _clean_value(item, depth + 1) for key, item in list(value.items())[:500]}
    return str(value)[:1000]


# --- 并发/超时/预算：可配置，读不到或非法一律回退安全默认 ---------------
DEFAULT_CONCURRENCY = 8      # 15 维在 8 并发下是 2 轮；旧值 3 是 5 轮
CONCURRENCY_MIN, CONCURRENCY_MAX = 1, 20
DEFAULT_DIM_TIMEOUT = 60     # 单维秒数；旧值跟随 llm["timeout"] = 300
DIM_TIMEOUT_MIN, DIM_TIMEOUT_MAX = 1, 120
DEFAULT_TOTAL_BUDGET = 150.0  # 整批秒数，超预算的维度不再发起调用
TOTAL_BUDGET_MIN, TOTAL_BUDGET_MAX = 1.0, 600.0
# to_thread 不能取消：留给 urllib 自己超时返回的余量，超过就放弃等待该维度。
_TIMEOUT_GRACE = 5.0

_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    """专用线程池：默认池 max_workers 可能小于并发数，会把并发压回 CPU 核数。

    并发上限 20，池子开 24，保证 `Semaphore(N)` 才是真正的并发闸门。
    """
    global _EXECUTOR
    if _EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _EXECUTOR is None:
                _EXECUTOR = ThreadPoolExecutor(
                    max_workers=CONCURRENCY_MAX + 4,
                    thread_name_prefix="interpret-dim",
                )
    return _EXECUTOR


def _coerce_int(raw: Any) -> int | None:
    """按整数解析；bool/空/非数字/非整数一律 None（交给调用方回退）。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        text = str(raw).strip()
    except Exception:  # noqa: BLE001 - 任何取不到的配置都算非法
        return None
    if not text:
        return None
    try:
        return int(text, 10)
    except (TypeError, ValueError):
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None
        if number != int(number):
            return None
        return int(number)


def _resolve_concurrency(raw: Any) -> int:
    """1~20，缺省/非法（0、负数、"abc"）回退 8；绝不把 0 传给 Semaphore。"""
    value = _coerce_int(raw)
    if value is None or value < CONCURRENCY_MIN or value > CONCURRENCY_MAX:
        return DEFAULT_CONCURRENCY
    return value


def _resolve_timeout(raw: Any) -> int:
    """1~120 秒，缺省/非法回退 60；超过上限按上限截断而不是丢弃。"""
    value = _coerce_int(raw)
    if value is None or value < DIM_TIMEOUT_MIN:
        return DEFAULT_DIM_TIMEOUT
    return min(value, DIM_TIMEOUT_MAX)


def _resolve_budget(raw: Any) -> float:
    """整批总预算（秒），缺省/非法回退 150，上限 600。"""
    if raw is None or isinstance(raw, bool):
        return DEFAULT_TOTAL_BUDGET
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_TOTAL_BUDGET
    if value != value or value < TOTAL_BUDGET_MIN:  # NaN 或非正数
        return DEFAULT_TOTAL_BUDGET
    return min(value, TOTAL_BUDGET_MAX)


def _settings_default(name: str, fallback: Any) -> Any:
    """从 core.config 的 Settings 取默认值；取不到（未安装/导入失败）用常量。"""
    try:
        from zhibiao.core.config import settings  # 延迟导入，避免配置模块与解读模块相互牵连

        value = getattr(settings, name, None)
    except Exception:  # noqa: BLE001 - 配置层异常绝不能弄挂解读
        return fallback
    return fallback if value is None else value


def resolve_controls(llm: dict[str, Any] | None) -> tuple[int, int, float]:
    """一次读出三个闸门参数（并发、单维超时、整批预算），保证同一次运行内自洽。

    优先级：llm 配置里的显式键 > Settings/环境变量 > 模块常量。
    """
    cfg = llm if isinstance(llm, dict) else {}

    def pick(key: str, setting: str) -> Any:
        raw = cfg.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raw = _settings_default(setting, None)
        return raw

    concurrency = _resolve_concurrency(pick("interpret_detail_concurrency", "INTERPRET_DETAIL_CONCURRENCY"))
    timeout = _resolve_timeout(pick("interpret_detail_timeout", "INTERPRET_DETAIL_TIMEOUT"))
    budget = _resolve_budget(pick("interpret_detail_budget", "INTERPRET_DETAIL_BUDGET"))
    return concurrency, timeout, budget


class DetailResults(list):
    """15 维结果列表（仍是 list，json 可直出），附带跳过汇总与本次闸门参数。

    线上要求「跳过要对外可见」：既不静默失败，也不为了一个维度把整份解读拖住。
    """

    skipped_dimensions: list[dict[str, Any]]
    notice: str
    elapsed_ms: float
    concurrency: int
    timeout: int
    budget: float

    def __init__(self, items: list[dict[str, Any]], *, skipped: list[dict[str, Any]],
                 elapsed_ms: float, concurrency: int, timeout: int, budget: float) -> None:
        super().__init__(items)
        self.skipped_dimensions = list(skipped)
        self.elapsed_ms = float(elapsed_ms)
        self.concurrency = int(concurrency)
        self.timeout = int(timeout)
        self.budget = float(budget)
        count = len(self.skipped_dimensions)
        self.notice = f"本次有 {count} 个维度未解析，已跳过" if count else ""


def _skipped_result(dimension: dict[str, Any], reason: str, detail: str,
                    elapsed_ms: float) -> dict[str, Any]:
    """统一的「跳过」结果：结构与正常维度一致，状态单独标出来。"""
    schema = dimension["schema"]
    return {
        "id": dimension["id"], "name": dimension["name"],
        "fields": {key: None for key in schema},
        "field_labels": {key: re.split(r"[（(]", str(label), maxsplit=1)[0] for key, label in schema.items()},
        "source_excerpt": "",
        "status": "skipped",
        "skipped": True,
        "skip_reason": reason,
        "error": detail,
        "elapsed_ms": round(max(0.0, elapsed_ms), 1),
    }


async def extract_detailed_interpretation(
    text: str,
    llm: dict[str, Any],
    call_llm: Callable[..., tuple[bool, str]],
    parse_json_map: Callable[[str], dict[str, Any]],
    scores: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Extract all 15 dimensions independently so found fields are enriched too.

    并发/超时/预算都可配置（见 resolve_controls）。旧实现是 `Semaphore(3)` +
    `timeout=llm["timeout"]`（默认 300 秒），15 次调用拆成 5 轮串行，任一维度
    卡住就把整次解读拖成分钟级；现在单维失败只跳过该维，不拖垮整批。
    """
    configured = llm_ready(llm)
    concurrency, timeout, budget = resolve_controls(llm)
    started_at = time.monotonic()
    deadline = started_at + budget
    semaphore = asyncio.Semaphore(concurrency)

    def remaining() -> float:
        return deadline - time.monotonic()

    async def extract_one(dimension: dict[str, Any]) -> dict[str, Any]:
        excerpt = _relevant_excerpt(text, dimension["keywords"])
        schema = dimension["schema"]
        result: dict[str, Any] = {
            "id": dimension["id"], "name": dimension["name"],
            "fields": {key: None for key in schema},
            "field_labels": {key: re.split(r"[（(]", str(label), maxsplit=1)[0] for key, label in schema.items()},
            "source_excerpt": excerpt[:1800],
            "status": "model_unavailable" if not configured else "pending",
            "error": "平台未启用大模型，保留了原文片段供人工查看" if not configured else "",
        }
        if not configured:
            return result
        if not excerpt.strip():
            result["status"] = "no_source"
            result["error"] = "未找到相关原文"
            return result

        system = (
            f"你是招投标文件信息抽取助手，当前只处理「{dimension['name']}」。"
            "只能依据用户提供的招标文件片段，不得调用常识补全或编造。"
            "必须返回一个 JSON 对象，键名严格使用给定的英文键。"
            "有内容时尽量逐项、完整摘录原文，尤其保留数字、单位、条件、条款号、档次和上下文；"
            "字段可以是字符串或数组，复杂评分项/流程用对象数组；未明确出现填 null。"
            "禁止把缺失信息猜成常见做法。竞争分析须区分文件事实与建议，不得臆造企业名称、份额或我方能力。"
        )
        prompt = (
            f"按以下字段解读招标文件。字段定义：\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
            f"本维度要求：{dimension['instruction']}\n"
            "返回 JSON 示例（字段值按实际情况填字符串、数组或 null）：\n"
            f"{json.dumps({key: None for key in schema}, ensure_ascii=False)}\n\n"
            f"招标文件相关原文：\n{excerpt}"
        )
        dim_started = time.monotonic()

        def elapsed() -> float:
            return (time.monotonic() - dim_started) * 1000

        async with semaphore:
            # 预算判断放在拿到槽位之后：排队期间预算可能已耗尽，此时不再发起新调用。
            if remaining() <= 0:
                return _skipped_result(dimension, "预算耗尽（budget_exhausted）",
                                       f"整批预算 {budget:.0f}s 已耗尽，该维度未发起调用", elapsed())
            attempts = 2  # 首次 + 重试 1 次
            ok, raw, failure = False, "", ""
            for attempt in range(attempts):
                left = remaining()
                if left <= 0:
                    failure = "预算耗尽（budget_exhausted）"
                    break
                eff_timeout = max(1, int(min(timeout, left)))
                cfg = dict(llm)
                cfg["temperature"] = 0.1
                # 重试由本函数统一负责；关掉 call_llm 内部的三连试，
                # 否则单维最坏耗时 = 3×timeout + 8s 退避，护栏形同虚设。
                cfg["retry"] = 0
                try:
                    ok, raw = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            _executor(), call_llm, cfg, prompt, eff_timeout, system),
                        timeout=eff_timeout + _TIMEOUT_GRACE,
                    )
                except asyncio.TimeoutError:
                    # to_thread 不可取消：停止等待、不重试，避免超时耗时翻倍。
                    return _skipped_result(
                        dimension, "timeout",
                        f"单维 {eff_timeout}s 未返回，已跳过（停止等待，不重试）", elapsed())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 单维异常绝不能让整次解读失败
                    ok, raw = False, f"模型调用失败：{exc}"
                if ok:
                    break
                failure = str(raw or "模型未返回正文")
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(1.0, max(0.0, remaining())))

            if not ok:
                reason = ("预算耗尽（budget_exhausted）" if "预算耗尽" in failure else "call_failed")
                return _skipped_result(dimension, reason,
                                       f"重试 1 次仍失败，已跳过：{failure[:200]}", elapsed())

        parsed = parse_json_map(raw)
        if not parsed:
            result["status"] = "invalid_response"
            result["error"] = "模型未返回可解析的 JSON；请重新解读"
            result["elapsed_ms"] = round(elapsed(), 1)
            return result
        fields = {key: _clean_value(parsed.get(key)) for key in schema}
        # 评分明细已有带来源的专用抽取器；将其完整结果合并，避免深度解读丢掉评分档次/出处。
        if dimension["id"] == "scoring" and scores:
            fields["scoring_items"] = [
                {key: item.get(key) for key in ("name", "score", "criteria", "source", "part") if item.get(key) not in (None, "")}
                for item in scores if isinstance(item, dict) and item.get("name")
            ] or fields.get("scoring_items")
        result["fields"] = fields
        result["filled_count"] = sum(value not in (None, "", [], {}) for value in fields.values())
        result["status"] = "completed" if result["filled_count"] else "no_fields_found"
        result["error"] = ""
        result["elapsed_ms"] = round(elapsed(), 1)
        return result

    # return_exceptions：任何漏网异常也只影响它自己的维度，不掀翻整次解读。
    raw_results = await asyncio.gather(
        *(extract_one(dimension) for dimension in DETAIL_DIMENSIONS),
        return_exceptions=True,
    )
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for dimension, item in zip(DETAIL_DIMENSIONS, raw_results):
        if isinstance(item, BaseException):
            item = _skipped_result(dimension, "exception",
                                   f"维度抽取异常，已跳过：{type(item).__name__}: {item}"[:300],
                                   (time.monotonic() - started_at) * 1000)
        results.append(item)
        if item.get("status") == "skipped":
            skipped.append({
                "key": item.get("id"), "name": item.get("name"),
                "reason": item.get("skip_reason") or "skipped",
                "detail": item.get("error") or "",
                "elapsed_ms": item.get("elapsed_ms"),
            })
    return DetailResults(results, skipped=skipped,
                         elapsed_ms=(time.monotonic() - started_at) * 1000,
                         concurrency=concurrency, timeout=timeout, budget=budget)
