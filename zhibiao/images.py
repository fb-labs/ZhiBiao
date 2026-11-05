"""标书生图：把正文里的示意图占位框换成真实图片。

只在「模型服务 → 能力开关 → 标书生图」开启时执行：
* 只处理**示意图/流程图/架构图**类占位框；证照、合同、截图等扫描件类图框
  一律保留占位，避免生成伪造证明材料。
* 生成结果落到 `var/images/<uuid>.png`，正文写成 `![说明](/api/images/<uuid>)`，
  导出 DOCX 时按 id 取字节真正嵌入。
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from pathlib import Path

import httpx

from zhibiao.core.image_slots import image_slot_hint, is_image_slot_line
from zhibiao.settings import get_settings

logger = logging.getLogger(__name__)

IMAGE_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
IMAGE_MARK_RE = re.compile(r"!\[[^\]\r\n]*\]\(/api/images/(%s)\)" % IMAGE_ID_RE.pattern)

# 可以交给图像模型画的占位框（示意图类）。
_ILLUSTRATION_WORDS = (
    "架构图",
    "流程图",
    "示意图",
    "拓扑图",
    "时序图",
    "结构图",
    "组织图",
    "关系图",
    "路线图",
    "甘特图",
    "思维导图",
)
# 扫描件/真实材料类图框不允许生成；截图、照片这类词可能是示意图说明里
# 的通用提示（例如「软件界面截图须截全」），不能单独作为排除依据。
_SCAN_WORDS = (
    "扫描件",
    "复印件",
    "证书",
    "执照",
    "报告",
    "合同",
    "凭证",
    "发票",
    "身份证",
    "授权书",
    "盖章",
    "签字",
)


def image_root() -> Path:
    settings = get_settings()
    root = settings.path(settings.image_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def guess_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _extension(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime, ".png")


def save_image(data: bytes, mime: str = "") -> str:
    """保存图片并返回 id（文件名与正文引用都用它）。"""
    image_id = str(uuid.uuid4())
    mime = mime or guess_mime(data)
    target = image_root() / f"{image_id}{_extension(mime)}"
    target.write_bytes(data)
    return image_id


def load_image(image_id: str) -> bytes | None:
    if not IMAGE_ID_RE.fullmatch(str(image_id or "")):
        return None
    for path in image_root().glob(f"{image_id}.*"):
        if path.is_file():
            return path.read_bytes()
    return None


def collect_images(texts) -> dict[str, bytes]:
    """从若干段正文里收集 `/api/images/<id>` 并按 id 载入字节，用于导出嵌入。"""
    ids: set[str] = set()
    for text in texts:
        ids.update(IMAGE_MARK_RE.findall(str(text or "")))
    images: dict[str, bytes] = {}
    for image_id in ids:
        raw = load_image(image_id)
        if raw:
            images[image_id] = raw
    return images


def is_illustration_hint(hint: str) -> bool:
    text = str(hint or "").strip()
    if not text:
        return False
    if any(word in text for word in _SCAN_WORDS):
        return False
    return any(word in text for word in _ILLUSTRATION_WORDS)


def _caption(hint: str, fallback: str = "示意图") -> str:
    head = re.split(r"（|\(", str(hint or ""), maxsplit=1)[0].strip()
    head = head.strip("。.；;，, ")
    return (head or fallback)[:40]


def _prompt(hint: str) -> str:
    return (
        "绘制一张用于投标文件的示意插图，主题：%s。"
        "要求：横向构图，纯白背景，深蓝与浅灰配色，扁平矢量风格，线条清晰、留白充足；"
        "如画面包含文字，一律使用简体中文并保证可读；"
        "不得出现任何真实企业名称、标识、证书编号、公章、签名或可识别个人信息的伪造内容。"
        "只输出图片本身。" % str(hint or "")[:400]
    )


async def request_image(prompt: str, cfg: dict) -> bytes:
    """调用兼容 OpenAI 协议的图像生成接口，返回图片字节。"""
    base_url = str(cfg.get("baseUrl") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("生图接口未配置地址")
    api_key = str(cfg.get("apiKey") or "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: dict = {
        "prompt": prompt,
        "n": 1,
        "size": str(cfg.get("size") or "1024x1024"),
        "response_format": "b64_json",
    }
    if cfg.get("model"):
        body["model"] = str(cfg["model"])
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        response = await client.post(f"{base_url}/images/generations", json=body, headers=headers)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data") or payload.get("images") or []
        if not items:
            raise RuntimeError("生图接口未返回图片")
        item = items[0] if isinstance(items[0], dict) else {"url": items[0]}
        raw = item.get("b64_json") or item.get("b64") or item.get("image_base64")
        if raw:
            return base64.b64decode(str(raw))
        url = item.get("url") or item.get("image_url")
        if url:
            image = await client.get(str(url))
            image.raise_for_status()
            return image.content
    raise RuntimeError("生图接口返回格式无法识别")


async def fill_image_slots(body: str, cfg: dict, budget: int) -> tuple[str, int]:
    """把可生成的示意图占位框替换成图片；返回 (新正文, 生成数量)。"""
    text = str(body or "")
    if not text or budget <= 0:
        return text, 0
    out: list[str] = []
    used = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped):]
        if used >= budget or not is_image_slot_line(stripped):
            out.append(line)
            continue
        hint = image_slot_hint(stripped)
        if not is_illustration_hint(hint):
            out.append(line)
            continue
        try:
            data = await request_image(_prompt(hint), cfg)
            image_id = save_image(data, guess_mime(data))
        except Exception as exc:  # noqa: BLE001 - 单张失败保留占位，不影响出稿
            logger.warning("标书生图失败，保留占位框：%s", exc)
            out.append(line)
            continue
        out.append("![%s](/api/images/%s)%s" % (_caption(hint), image_id, ending))
        used += 1
    return "".join(out), used
