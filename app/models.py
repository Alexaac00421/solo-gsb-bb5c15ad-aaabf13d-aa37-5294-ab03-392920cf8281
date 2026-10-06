"""请求体的 Pydantic 模型（业务校验在路由内完成以给出明确错误码）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class RectIn(BaseModel):
    x: int
    y: int
    w: int
    h: int
    reason: str = Field(max_length=500)


class AnnotationsPut(BaseModel):
    """整体替换当前标注；revision 必须等于服务端当前修订号（乐观锁）。"""

    revision: int = Field(ge=0)
    rects: list[RectIn] = Field(default_factory=list, max_length=1000)


class BatchItem(BaseModel):
    """批次中的一项：文档 ID 与调用方预期的当前标注修订号。"""

    doc_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=0)


class MigrationRequest(BaseModel):
    """遮盖方案迁移请求：把源文档当前标注整批缩放到多个目标文档。

    携带源文档 ID、源当前标注修订号，以及 1～20 个不同目标文档 ID 与
    各自预期修订号（源不能作为目标）。数量、重复、源是否混入目标等
    语义校验在路由内完成，以返回统一格式的业务错误码。
    """

    source_doc_id: str = Field(min_length=1, max_length=64)
    source_revision: int = Field(ge=0)
    targets: list[BatchItem] = Field(default_factory=list)



class PublishRequest(BaseModel):
    """单份发布请求（整体可选）：可指定统一的 UTC 公开时间。

    省略请求体或 ``publish_at`` 省略/为 null 时仍立即公开；``publish_at``
    为 RFC 3339/ISO 8601 时间字符串，必须携带时区偏移，语义校验（晚于当前
    时刻且不超过 30 天、空白字符串等无效值）在路由内统一返回业务错误码。
    """

    publish_at: str | None = Field(default=None, max_length=64)


class BatchPublishRequest(BaseModel):
    """同批次发布请求：2～20 个不同文档，可携带幂等键与统一公开时间。

    数量、重复等语义校验在路由内完成，以返回统一格式的业务错误码。
    idempotency_key 为可选、调用方保证全局唯一的键；键与按请求顺序排列的
    (doc_id, expected_revision) 列表及统一的 publish_at 绑定。
    publish_at 省略/为 null 时整批立即公开。
    """

    documents: list[BatchItem] = Field(default_factory=list)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    publish_at: str | None = Field(default=None, max_length=64)


class WithdrawRequest(BaseModel):
    """撤回已发布版本：原因必填（去空白后为空则在路由内拒绝）。

    撤回是永久且幂等的：重复撤回同一版本返回首次记录，不改写原因。
    """

    reason: str = Field(max_length=500)


class PackageItem(BaseModel):
    """离线交付包中的一项：文档 ID 与已发布版本号（无需标注修订号）。"""

    doc_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)


class PackageRequest(BaseModel):
    """离线交付包请求：按请求顺序列出 2～20 个互不重复的 (doc_id, version)。

    数量、重复等语义校验在路由内完成，以返回统一格式的业务错误码。
    """

    items: list[PackageItem] = Field(default_factory=list)


class CoverageDiffRequest(BaseModel):
    """重发影像前的遮盖范围比较请求（纯只读）。

    携带调用方持有的**当前标注修订号**与作为比较基准的**已发布版本号**：
    修订号必须等于服务端当前修订号（过期即 409 stale_revision），基准版本
    必须存在、未撤回且已到公开时间（否则按公开读取同形态拒绝，且不泄露该
    版本的遮盖详情）。基准矩形取自该版本发布时冻结的标注修订，随后的标注
    修改不影响比较结果。
    """

    revision: int = Field(ge=0)
    baseline_version: int = Field(ge=1)
