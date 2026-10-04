"""Pydantic 输入/输出模型。

时间处理约定
------------
* 所有带时区的输入时间统一转换为 UTC 存储与比较；
* 不带时区的时间一律假定为 UTC（naive 视为 UTC，不使用服务器本地时区）；
* ``date`` 类型输入（仅日期）按该日 UTC 00:00:00 处理。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Optional

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)


# --------------------------------------------------------------------------- #
# 时间规范化
# --------------------------------------------------------------------------- #
def _coerce_to_utc(value):
    """将任意可解析的时间输入转换为 timezone-aware UTC datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        text = value.strip()
        # 仅日期 YYYY-MM-DD → UTC 00:00
        if len(text) == 10:
            parsed = date.fromisoformat(text)
            dt = datetime(parsed.year, parsed.month, parsed.day)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        raise ValueError(f"无法解析的时间值: {value!r}")

    if dt.tzinfo is None:
        # naive 时间一律假定为 UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, BeforeValidator(_coerce_to_utc)]


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class RelationType(str, Enum):
    """修订关系类型。

    * ``revises`` —— 新版本接续旧版本（形成可继续延伸的修订链）；
    * ``repeals`` —— 新条文明文废止旧条文（链的终止点）。
    """

    REVISES = "revises"
    REPEALS = "repeals"


class AuditStatus(str, Enum):
    """引用审查结论。"""

    EFFECTIVE = "effective"                    # 当日有效
    REPEALED = "repealed"                      # 已废止
    NOT_YET_EFFECTIVE = "not_yet_effective"    # 尚未生效
    AMBIGUOUS = "ambiguous"                    # 链分叉/有效版本不唯一
    CIRCULAR = "circular"                      # 修订链存在循环
    NOT_FOUND = "not_found"                    # 被引目标不存在
    BROKEN_CHAIN = "broken_chain"              # 指定版本/锚点不在链中


STATUS_LABELS: dict[AuditStatus, str] = {
    AuditStatus.EFFECTIVE: "当日有效",
    AuditStatus.REPEALED: "已废止",
    AuditStatus.NOT_YET_EFFECTIVE: "尚未生效",
    AuditStatus.AMBIGUOUS: "歧义",
    AuditStatus.CIRCULAR: "循环",
    AuditStatus.NOT_FOUND: "目标不存在",
    AuditStatus.BROKEN_CHAIN: "修订链断裂",
}


# --------------------------------------------------------------------------- #
# 输入模型
# --------------------------------------------------------------------------- #
class ClauseIn(BaseModel):
    """一个条款版本及其生效/失效时间。"""

    model_config = ConfigDict(extra="forbid")

    clause_id: str = Field(..., min_length=1, description="条文编号，如 第10条")
    title: Optional[str] = Field(None, description="条文标题，可选")
    text: Optional[str] = Field("", description="条文文本，可选")
    effective_at: Optional[UtcDatetime] = Field(
        None, description="生效时间；缺省继承文档版本生效时间"
    )
    expires_at: Optional[UtcDatetime] = Field(
        None, description="失效时间；缺省表示长期有效（或由废止关系终止）"
    )

    @field_validator("expires_at")
    @classmethod
    def _expiry_after_effective(cls, v, info):
        effective = info.data.get("effective_at")
        if v is not None and effective is not None and v <= effective:
            raise ValueError("expires_at 必须晚于 effective_at")
        return v


class RevisionIn(BaseModel):
    """一条修订关系（同一文档版本写入批次内登记）。"""

    model_config = ConfigDict(extra="forbid")

    kind: RelationType = Field(..., description="修订类型：revises / repeals")
    # 被修订/被废止的旧条文节点
    predecessor_doc: str = Field(..., min_length=1)
    predecessor_version: str = Field(..., min_length=1)
    predecessor_clause: str = Field(..., min_length=1)
    # 接续/废止的新条文节点；repeals 且省略时表示无接续的纯废止
    successor_doc: Optional[str] = Field(None, min_length=1)
    successor_version: Optional[str] = Field(None, min_length=1)
    successor_clause: Optional[str] = Field(None, min_length=1)
    note: Optional[str] = Field(None, description="修订说明，可选")

    @field_validator("successor_doc", "successor_version", "successor_clause")
    @classmethod
    def _successor_all_or_none(cls, v, info):
        return v


class CitationIn(BaseModel):
    """一条引用：源条文 -> 目标条文。"""

    model_config = ConfigDict(extra="forbid")

    source_clause: str = Field(..., min_length=1, description="引用方条文编号（须属于本批次文档版本）")
    target_doc: str = Field(..., min_length=1, description="被引文档标识")
    target_clause: str = Field(..., min_length=1, description="被引条文编号")
    target_version: Optional[str] = Field(
        None,
        description=(
            "被引具体版本：指定则按“固定版本引用”解析，"
            "缺省则沿修订链解析到目标日期的最新有效版本"
        ),
    )
    context: Optional[str] = Field(None, description="引用语境/原文，可选")


class CorpusIn(BaseModel):
    """POST /corpus 的写入载荷：一个文档版本及其条款、修订关系、引用。"""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(..., min_length=1, description="文档标识，如 LAW-CONTRACT")
    version: str = Field(..., min_length=1, description="文档版本，如 2021 或 v2")
    name: Optional[str] = Field(None, description="文档名称，可选")
    effective_at: Optional[UtcDatetime] = Field(
        None, description="文档版本生效时间，条款未单独指定时继承"
    )
    clauses: list[ClauseIn] = Field(default_factory=list)
    revisions: list[RevisionIn] = Field(default_factory=list)
    citations: list[CitationIn] = Field(default_factory=list)

    @field_validator("clauses")
    @classmethod
    def _unique_clause(cls, v):
        ids = [c.clause_id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("同一文档版本内 clause_id 不得重复")
        return v


class AuditRequest(BaseModel):
    """POST /audit 的请求体（全部可选，默认审查全部存量引用）。"""

    model_config = ConfigDict(extra="forbid")

    target_at: Optional[UtcDatetime] = Field(
        None, description="目标日期时间；缺省为当前 UTC 时间；仅日期按 UTC 00:00"
    )
    source_doc: Optional[str] = Field(None, description="仅审查该文档版本发出的引用")
    source_version: Optional[str] = Field(None, description="仅审查该版本发出的引用")
    source_clause: Optional[str] = Field(None, description="仅审查该源条文发出的引用")
    target_doc: Optional[str] = Field(None, description="仅审查指向该文档的引用")


class RehearsalRequest(BaseModel):
    """POST /rehearse 的请求体：拟登记版本 + 预演目标日期（+ 可选过滤）。

    ``candidate`` 的格式与 ``POST /corpus`` 请求体完全一致，复用其全部
    校验与错误响应；预演不会真正写入任何数据。
    """

    model_config = ConfigDict(extra="forbid")

    candidate: CorpusIn = Field(
        ..., description="拟登记的文档版本（字段与 POST /corpus 请求体相同）"
    )
    target_at: Optional[UtcDatetime] = Field(
        None,
        description="预演审查的目标日期时间；缺省为当前 UTC 时间；仅日期按 UTC 00:00",
    )
    source_doc: Optional[str] = Field(None, description="仅比较该文档版本发出的引用")
    source_version: Optional[str] = Field(None, description="仅比较该版本发出的引用")
    source_clause: Optional[str] = Field(None, description="仅比较该源条文发出的引用")
    target_doc: Optional[str] = Field(None, description="仅比较指向该文档的引用")
