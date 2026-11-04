from __future__ import annotations


class TenderStatus:
    INTAKE = "intake"
    ANALYZED = "analyzed"
    OUTLINED = "outlined"
    DRAFTING = "drafting"
    REVIEW = "review"
    DELIVERY = "delivery"
    COMPLETE = "complete"
    ARCHIVED = "archived"

    ALL = (INTAKE, ANALYZED, OUTLINED, DRAFTING, REVIEW, DELIVERY, COMPLETE, ARCHIVED)


class FileRole:
    TENDER = "tender"
    RESPONSE = "response"
    TEMPLATE = "template"
    ATTACHMENT = "attachment"

    ALL = (TENDER, RESPONSE, TEMPLATE, ATTACHMENT)


class JobKind:
    ANALYZE = "analyze"
    OUTLINE = "outline"
    WRITE = "write"
    REVIEW = "review"
    DELIVER = "deliver"


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class ReviewMode:
    RULE = "rule"
    AI = "ai"
    MIXED = "mixed"


class Severity:
    BLOCKER = "blocker"
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class DeliveryFormat:
    DOCX = "docx"
    PDF = "pdf"


PERMISSION_CATALOG = (
    ("tender.read", "查看投标项目", "投标项目"),
    ("tender.write", "新建与编辑投标项目", "投标项目"),
    ("analysis.run", "执行招标解读", "投标项目"),
    ("document.write", "编写与生成标书正文", "标书编写"),
    ("review.run", "执行质量检查", "质量检查"),
    ("delivery.run", "生成交付文件", "文档交付"),
    ("admin.ai", "管理模型服务", "平台管理"),
    ("admin.accounts", "管理账号与角色", "平台管理"),
    ("admin.templates", "管理文档模板", "平台管理"),
)

ROLE_CATALOG = (
    ("admin", "系统管理员", "拥有全部权限"),
    ("manager", "投标经理", "管理项目、解读、检查与交付"),
    ("editor", "标书编辑", "编写与生成标书正文"),
    ("reviewer", "评标审核", "查看项目并执行质量检查"),
)

ROLE_PERMISSIONS = {
    "admin": [code for code, _, _ in PERMISSION_CATALOG],
    "manager": [
        "tender.read",
        "tender.write",
        "analysis.run",
        "document.write",
        "review.run",
        "delivery.run",
    ],
    "editor": ["tender.read", "document.write"],
    "reviewer": ["tender.read", "review.run"],
}
