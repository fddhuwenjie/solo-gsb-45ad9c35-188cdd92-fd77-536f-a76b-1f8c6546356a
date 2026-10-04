"""FastAPI 入口：法规修订链引用审查 API。

端点
----
* ``POST /corpus``            写入一个文档版本（条款/修订关系/引用），重复写入返回 409
* ``GET  /corpus/{doc}/{ver}``查看已写入的文档版本
* ``POST /audit``             按目标日期解析引用，分类并返回可追溯链路
* ``POST /rehearse``          登记前预演：比较拟登记版本假定登记前后的审查结果，不落库
* ``GET  /health``            健康检查
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import database as db
from .resolver import Graph, run_audit
from .response import AuditResponse, CorpusResponse, HealthResponse, RehearseResponse
from .schemas import AuditRequest, CorpusIn, RehearsalRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    conn = sqlite3.connect(db.get_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    app.state.conn = conn
    try:
        yield
    finally:
        conn.close()


app = FastAPI(
    title="法规修订链引用审查 API",
    version="1.0.0",
    description=(
        "沿可分支的法规修订链解析条款引用，区分当日有效、已废止、"
        "尚未生效、歧义与循环，并返回完整可追溯链路。"
    ),
    lifespan=lifespan,
)

# 单条共享 SQLite 连接被多线程复用；用 RLock 串行化所有游标访问
# （WAL 保证与容器外其它连接的读并发；本服务内请求量级下足够）。
_db_lock = threading.RLock()


def get_conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """业务校验错误统一为 422。"""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health(conn: sqlite3.Connection = Depends(get_conn)):
    with _db_lock:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "database": db.get_db_path()}


@app.post(
    "/corpus",
    response_model=CorpusResponse,
    status_code=201,
    tags=["corpus"],
    summary="写入一个文档版本（幂等：相同文档版本重复写入返回 409）",
)
def create_corpus(payload: CorpusIn, conn: sqlite3.Connection = Depends(get_conn)):
    with _db_lock:
        existing = db.document_version_exists(
            conn, payload.doc_id, payload.version
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"文档版本 {payload.doc_id}@{payload.version} 已写入，"
                    "相同文档版本不得重复写入；如需修订请使用新的版本号"
                ),
            )
        try:
            result = db.insert_corpus(conn, payload)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "doc_id": payload.doc_id,
        "version": payload.version,
        "name": payload.name,
        "effective_at": (
            payload.effective_at.isoformat() if payload.effective_at else None
        ),
        "created": True,
        "counts": {
            "clauses": result["clauses"],
            "revisions": result["revisions"],
            "citations": result["citations"],
        },
        "warnings": result["warnings"],
    }


@app.get(
    "/corpus/{doc_id}/{version}",
    tags=["corpus"],
    summary="查看已写入的文档版本详情",
)
def get_corpus(doc_id: str, version: str,
               conn: sqlite3.Connection = Depends(get_conn)):
    with _db_lock:
        doc = db.fetch_document(conn, doc_id, version)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档版本不存在")
    return doc


@app.post(
    "/audit",
    response_model=AuditResponse,
    tags=["audit"],
    summary="按目标日期解析全部（或过滤后的）引用并分类",
)
def audit(
    request: Optional[AuditRequest] = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    req = request or AuditRequest()
    target = req.target_at or datetime.now(timezone.utc)

    with _db_lock:
        citations = db.list_citations(
            conn,
            source_doc=req.source_doc,
            source_version=req.source_version,
            source_clause=req.source_clause,
            target_doc=req.target_doc,
        )
        graph = Graph(db.list_clauses(conn), db.list_edges(conn))
    return run_audit(graph, citations, target)


def _audit_from_snapshots(clause_rows, edge_rows, citation_rows, target):
    """对库状态快照（Row 列表）执行一次审查，与 /audit 同一解析逻辑。"""
    graph = Graph(list(clause_rows), list(edge_rows))
    return run_audit(graph, list(citation_rows), target)


@app.post(
    "/rehearse",
    response_model=RehearseResponse,
    tags=["rehearse"],
    summary=(
        "登记前预演：提交拟登记版本与目标日期，比较库中原引用在登记前与"
        "假定登记后的审查结果（不写入任何数据）"
    ),
)
def rehearse(
    request: RehearsalRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    payload = request.candidate
    target = request.target_at or datetime.now(timezone.utc)

    with _db_lock:
        # 重复版本沿用 /corpus 的 409 错误响应（此时尚未开启写事务）
        existing = db.document_version_exists(conn, payload.doc_id, payload.version)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"文档版本 {payload.doc_id}@{payload.version} 已写入，"
                    "相同文档版本不得重复写入；如需修订请使用新的版本号"
                ),
            )

        # 预演前的库状态快照：存量引用（拟登记版本自带引用稍后才出现，
        # 自然不纳入比较），以及前后审查共用的条款/关系基线。
        before_citations = db.list_citations(
            conn,
            source_doc=request.source_doc,
            source_version=request.source_version,
            source_clause=request.source_clause,
            target_doc=request.target_doc,
        )
        base_clauses = db.list_clauses(conn)
        base_edges = db.list_edges(conn)

        # 假定登记：复用 /corpus 的同一写入与校验路径（含非法关系的 422），
        # 但绝不 commit；finally 中一律 rollback，预演成功或失败都不留痕。
        try:
            result = db.insert_corpus(conn, payload)
            after_clauses = db.list_clauses(conn)
            after_edges = db.list_edges(conn)

            before_audit = _audit_from_snapshots(
                base_clauses, base_edges, before_citations, target
            )
            # 前后审查基于同一份库状态：引用集合固定为登记前的存量引用
            after_audit = _audit_from_snapshots(
                after_clauses, after_edges, before_citations, target
            )
        finally:
            conn.rollback()

    changes = _diff_audits(before_audit["results"], after_audit["results"])
    return {
        "candidate": {
            "doc_id": payload.doc_id,
            "version": payload.version,
        },
        "target_at": target.isoformat(),
        "total": before_audit["total"],
        "changed": len(changes),
        "changes": changes,
        "projected_counts": {
            "clauses": result["clauses"],
            "revisions": result["revisions"],
            "citations": result["citations"],
        },
        "projected_warnings": result["warnings"],
    }


def _chain_fingerprint(result: dict) -> str:
    """完整链路（含每步状态/边/终止步）的稳定指纹，用于判定链路是否变化。"""
    return json.dumps(
        result.get("chains", []),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _resolved_fingerprint(result: dict) -> Optional[str]:
    resolved = result.get("resolved")
    return None if resolved is None else json.dumps(
        resolved, sort_keys=True, separators=(",", ":")
    )


def _diff_audits(before_results: list[dict], after_results: list[dict]) -> list[dict]:
    """按原引用 ID 配对，仅保留结论/解析版本/链路变化的引用，按 ID 升序。"""
    after_by_id = {r["citation_id"]: r for r in after_results}
    changes: list[dict] = []
    for before in before_results:
        after = after_by_id.get(before["citation_id"])
        if after is None:
            continue
        changed_fields: list[str] = []
        if after["status"] != before["status"]:
            changed_fields.append("status")
        if _resolved_fingerprint(after) != _resolved_fingerprint(before):
            changed_fields.append("resolved")
        if _chain_fingerprint(after) != _chain_fingerprint(before):
            changed_fields.append("chains")
        if changed_fields:
            changes.append(
                {
                    "citation_id": before["citation_id"],
                    "changes": changed_fields,
                    "before": before,
                    "after": after,
                }
            )
    changes.sort(key=lambda c: c["citation_id"])
    return changes
