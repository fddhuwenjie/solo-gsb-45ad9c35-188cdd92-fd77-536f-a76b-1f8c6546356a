"""响应模型（OpenAPI 文档与输出校验）。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class NodeRef(BaseModel):
    doc_id: str
    version: str
    clause_id: str


class SourceRef(BaseModel):
    doc_id: str
    version: str
    clause_id: str


class TargetRef(BaseModel):
    doc_id: str
    clause_id: str
    pinned_version: Optional[str] = None


class RecordedBy(BaseModel):
    doc_id: str
    version: str


class ChainStep(BaseModel):
    node: Optional[NodeRef]
    title: Optional[str] = None
    effective_at: Optional[str] = None
    expires_at: Optional[str] = None
    state_at_target: str
    edge_in: Optional[str] = Field(
        None, description="进入本节点的修订关系：revises / repeals；根节点为 null"
    )
    terminal: bool = False
    note: Optional[str] = None
    recorded_by: Optional[RecordedBy] = None
    exists_in_corpus: bool = True


class Chain(BaseModel):
    anchor: NodeRef
    branch_index: int
    steps: list[ChainStep]


class CitationAudit(BaseModel):
    citation_id: int
    source: SourceRef
    target: TargetRef
    pinned: bool
    context: Optional[str]
    target_at: str
    status: str
    status_label: str
    reason: str
    resolved: Optional[NodeRef]
    effective_candidates: list[NodeRef]
    cycle: Optional[list[NodeRef]]
    anchors: list[NodeRef]
    chains: list[Chain]


class AuditResponse(BaseModel):
    target_at: str
    total: int
    summary: dict[str, int]
    results: list[CitationAudit]


class CorpusResponse(BaseModel):
    doc_id: str
    version: str
    name: Optional[str]
    effective_at: Optional[str]
    created: bool
    counts: dict[str, int]
    warnings: list[str]


class RehearseChangedCitation(BaseModel):
    """一条前后结论/解析版本/链路发生变化的存量引用。"""

    citation_id: int
    changes: list[str] = Field(
        ..., description="变化项：status / resolved / chains"
    )
    before: CitationAudit = Field(
        ..., description="假定登记前、基于当前库状态的完整审查结果"
    )
    after: CitationAudit = Field(
        ..., description="假定登记后、基于同一份库状态叠加拟登记版本的完整审查结果"
    )


class RehearseResponse(BaseModel):
    candidate: dict
    target_at: str
    total: int = Field(..., description="参与比较的存量引用数量（不含拟登记版本自带引用）")
    changed: int = Field(..., description="结论/解析版本/链路发生变化的引用数量")
    changes: list[RehearseChangedCitation] = Field(
        ..., description="变化引用清单，按原引用 ID 升序"
    )
    projected_counts: dict[str, int] = Field(
        ..., description="拟登记版本若正式登记时返回的写入计数"
    )
    projected_warnings: list[str] = Field(
        ..., description="拟登记版本若正式登记时产生的 warnings"
    )


class HealthResponse(BaseModel):
    status: str
    database: str
