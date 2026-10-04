"""SQLite 持久化层。

每个请求从同一连接（``check_same_thread=False``）获取游标；
FastAPI 依赖中加锁，保证多线程下的写入安全。
所有时间以 ISO-8601 UTC 字符串存储。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Optional

from .schemas import CorpusIn, RevisionIn

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS document_versions (
    doc_id       TEXT NOT NULL,
    version      TEXT NOT NULL,
    name         TEXT,
    effective_at TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (doc_id, version)
);

CREATE TABLE IF NOT EXISTS clause_versions (
    doc_id       TEXT NOT NULL,
    version      TEXT NOT NULL,
    clause_id    TEXT NOT NULL,
    title        TEXT,
    text         TEXT,
    effective_at TEXT,
    expires_at   TEXT,
    PRIMARY KEY (doc_id, version, clause_id),
    FOREIGN KEY (doc_id, version) REFERENCES document_versions(doc_id, version)
);

CREATE TABLE IF NOT EXISTS revision_edges (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    kind               TEXT NOT NULL,             -- revises | repeals
    predecessor_doc    TEXT NOT NULL,
    predecessor_version TEXT NOT NULL,
    predecessor_clause TEXT NOT NULL,
    successor_doc      TEXT,
    successor_version  TEXT,
    successor_clause   TEXT,
    recorded_by_doc    TEXT NOT NULL,
    recorded_by_version TEXT NOT NULL,
    note               TEXT
);

-- 同一条修订关系（含纯废止，COALESCE 统一 NULL）不得重复登记
CREATE UNIQUE INDEX IF NOT EXISTS ux_revision_edges ON revision_edges (
    kind,
    predecessor_doc, predecessor_version, predecessor_clause,
    COALESCE(successor_doc, ''),
    COALESCE(successor_version, ''),
    COALESCE(successor_clause, '')
);

CREATE TABLE IF NOT EXISTS citations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_doc     TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_clause  TEXT NOT NULL,
    target_doc     TEXT NOT NULL,
    target_clause  TEXT NOT NULL,
    target_version TEXT,
    context        TEXT
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone_utc()).isoformat()


def timezone_utc():
    from datetime import timezone

    return timezone.utc


def get_db_path() -> str:
    return os.environ.get("DB_PATH", "/data/regulations.db")


@contextmanager
def connect(db_path: Optional[str] = None):
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> None:
    """创建表结构（幂等）。"""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------- #
# 读取辅助
# --------------------------------------------------------------------------- #
def document_version_exists(
    conn: sqlite3.Connection, doc_id: str, version: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM document_versions WHERE doc_id = ? AND version = ?",
        (doc_id, version),
    ).fetchone()


def clause_exists(
    conn: sqlite3.Connection, doc: str, version: str, clause: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM clause_versions"
        " WHERE doc_id = ? AND version = ? AND clause_id = ?",
        (doc, version, clause),
    ).fetchone()
    return row is not None


def list_clauses(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT doc_id, version, clause_id, title, text,"
        " effective_at, expires_at FROM clause_versions"
    ).fetchall()


def list_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # 关联登记版本，取其生效时间：无接续的纯废止以该时刻为废止时刻
    return conn.execute(
        "SELECT e.kind, e.predecessor_doc, e.predecessor_version,"
        " e.predecessor_clause, e.successor_doc, e.successor_version,"
        " e.successor_clause, e.note, e.recorded_by_doc, e.recorded_by_version,"
        " dv.effective_at AS recorded_effective_at"
        " FROM revision_edges e"
        " LEFT JOIN document_versions dv"
        "   ON dv.doc_id = e.recorded_by_doc"
        "  AND dv.version = e.recorded_by_version"
    ).fetchall()


def list_citations(
    conn: sqlite3.Connection,
    source_doc: Optional[str] = None,
    source_version: Optional[str] = None,
    source_clause: Optional[str] = None,
    target_doc: Optional[str] = None,
) -> list[sqlite3.Row]:
    sql = (
        "SELECT id, source_doc, source_version, source_clause,"
        " target_doc, target_clause, target_version, context"
        " FROM citations WHERE 1=1"
    )
    params: list[Any] = []
    if source_doc is not None:
        sql += " AND source_doc = ?"
        params.append(source_doc)
    if source_version is not None:
        sql += " AND source_version = ?"
        params.append(source_version)
    if source_clause is not None:
        sql += " AND source_clause = ?"
        params.append(source_clause)
    if target_doc is not None:
        sql += " AND target_doc = ?"
        params.append(target_doc)
    sql += " ORDER BY id"
    return conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def insert_corpus(conn: sqlite3.Connection, payload: CorpusIn) -> dict:
    """幂等校验后写入一个文档版本。调用方需先做重复检查。"""
    warnings: list[str] = []
    doc_effective = (
        payload.effective_at.isoformat() if payload.effective_at else None
    )

    conn.execute(
        "INSERT INTO document_versions"
        " (doc_id, version, name, effective_at, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            payload.doc_id,
            payload.version,
            payload.name,
            doc_effective,
            utc_now_iso(),
        ),
    )

    for clause in payload.clauses:
        effective = clause.effective_at or payload.effective_at
        if clause.effective_at is None and payload.effective_at is None:
            # 两者都没有时不强制报错，但记录警告；审查时按“始终有效”处理
            warnings.append(
                f"条款 {clause.clause_id} 与文档版本均未提供 effective_at"
            )
        conn.execute(
            "INSERT INTO clause_versions"
            " (doc_id, version, clause_id, title, text,"
            " effective_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payload.doc_id,
                payload.version,
                clause.clause_id,
                clause.title,
                clause.text,
                effective.isoformat() if effective else None,
                clause.expires_at.isoformat() if clause.expires_at else None,
            ),
        )

    inserted_edges = 0
    for rev in payload.revisions:
        _validate_revision_nodes(payload, rev)
        cur = conn.execute(
            "INSERT OR IGNORE INTO revision_edges"
            " (kind, predecessor_doc, predecessor_version, predecessor_clause,"
            "  successor_doc, successor_version, successor_clause,"
            "  recorded_by_doc, recorded_by_version, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rev.kind.value,
                rev.predecessor_doc,
                rev.predecessor_version,
                rev.predecessor_clause,
                rev.successor_doc,
                rev.successor_version,
                rev.successor_clause,
                payload.doc_id,
                payload.version,
                rev.note,
            ),
        )
        inserted_edges += cur.rowcount

    for cite in payload.citations:
        if not clause_exists(
            conn, payload.doc_id, payload.version, cite.source_clause
        ):
            raise ValueError(
                f"引用源条文 {cite.source_clause} 不在本次写入的"
                f" {payload.doc_id}@{payload.version} 中"
            )
        conn.execute(
            "INSERT INTO citations"
            " (source_doc, source_version, source_clause,"
            "  target_doc, target_clause, target_version, context)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payload.doc_id,
                payload.version,
                cite.source_clause,
                cite.target_doc,
                cite.target_clause,
                cite.target_version,
                cite.context,
            ),
        )

    # 悬空引用警告（修订关系指向当前语料库中尚不存在的节点，允许先登记后补齐）
    warnings.extend(_dangling_warnings(conn, payload))

    return {
        "clauses": len(payload.clauses),
        "revisions": inserted_edges,
        "citations": len(payload.citations),
        "warnings": warnings,
    }


def _validate_revision_nodes(payload: CorpusIn, rev: RevisionIn) -> None:
    succ_fields = (
        rev.successor_doc,
        rev.successor_version,
        rev.successor_clause,
    )
    if any(f is not None for f in succ_fields) and not all(
        f is not None for f in succ_fields
    ):
        raise ValueError(
            "修订关系的 successor_doc/version/clause 必须同时提供，"
            "或全部省略（仅 repeals 可省略，表示纯废止）"
        )
    if rev.kind.value == "revises" and any(f is None for f in succ_fields):
        raise ValueError("revises 关系必须提供完整的 successor 节点")
    # 接续/废止节点原则上属于本次写入的新版本（自修订无意义）
    if rev.successor_doc is not None and (
        rev.successor_doc,
        rev.successor_version,
    ) != (payload.doc_id, payload.version):
        raise ValueError(
            "修订关系的 successor 必须属于本次写入的文档版本"
        )
    if rev.successor_clause is not None and not any(
        c.clause_id == rev.successor_clause for c in payload.clauses
    ):
        raise ValueError(
            f"修订关系 successor 条文 {rev.successor_clause}"
            " 不在本次写入的条款列表中"
        )


def _dangling_warnings(
    conn: sqlite3.Connection, payload: CorpusIn
) -> list[str]:
    warnings: list[str] = []
    for rev in payload.revisions:
        if not clause_exists(
            conn,
            rev.predecessor_doc,
            rev.predecessor_version,
            rev.predecessor_clause,
        ):
            warnings.append(
                "修订关系前序节点不存在于语料库: "
                f"{rev.predecessor_doc}@{rev.predecessor_version}"
                f"#{rev.predecessor_clause}"
            )
    for cite in payload.citations:
        if not _clause_id_exists(conn, cite.target_doc, cite.target_clause):
            warnings.append(
                "引用目标条文在语料库中不存在任何版本: "
                f"{cite.target_doc}#{cite.target_clause}"
            )
        elif cite.target_version is not None and not clause_exists(
            conn,
            cite.target_doc,
            cite.target_version,
            cite.target_clause,
        ):
            warnings.append(
                "引用目标固定版本不存在: "
                f"{cite.target_doc}@{cite.target_version}"
                f"#{cite.target_clause}"
            )
    return warnings


def _clause_id_exists(
    conn: sqlite3.Connection, doc: str, clause: str
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM clause_versions"
            " WHERE doc_id = ? AND clause_id = ? LIMIT 1",
            (doc, clause),
        ).fetchone()
        is not None
    )


def fetch_document(
    conn: sqlite3.Connection, doc_id: str, version: str
) -> Optional[dict]:
    doc = document_version_exists(conn, doc_id, version)
    if doc is None:
        return None
    clauses = conn.execute(
        "SELECT doc_id, version, clause_id, title, text,"
        " effective_at, expires_at FROM clause_versions"
        " WHERE doc_id = ? AND version = ? ORDER BY rowid",
        (doc_id, version),
    ).fetchall()
    edges = conn.execute(
        "SELECT kind, predecessor_doc, predecessor_version,"
        " predecessor_clause, successor_doc, successor_version,"
        " successor_clause, note FROM revision_edges"
        " WHERE recorded_by_doc = ? AND recorded_by_version = ?",
        (doc_id, version),
    ).fetchall()
    cites = conn.execute(
        "SELECT source_clause, target_doc, target_clause,"
        " target_version, context FROM citations"
        " WHERE source_doc = ? AND source_version = ? ORDER BY id",
        (doc_id, version),
    ).fetchall()
    return {
        "doc_id": doc["doc_id"],
        "version": doc["version"],
        "name": doc["name"],
        "effective_at": doc["effective_at"],
        "clauses": [dict(r) for r in clauses],
        "revisions": [dict(r) for r in edges],
        "citations": [dict(r) for r in cites],
    }
