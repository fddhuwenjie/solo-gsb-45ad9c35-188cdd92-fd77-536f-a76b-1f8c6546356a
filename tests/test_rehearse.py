"""端到端测试：POST /rehearse 登记前预演。

覆盖：
* 修订关系跨文档/跨条号改变旧引用的结论、解析版本与链路；
* 纯废止、固定引用沿用 /audit 判定；
* 无变化时 changes 为空；
* 预演成功或失败均不留任何文档/条款/关系/引用，之后仍可正式登记；
* 重复版本 409、非法关系 422，沿用 /corpus 错误响应；
* 拟登记版本自带引用不纳入比较；
* 结果按原引用 ID 升序；过滤参数生效；
* 并发登记：基线在同一只读快照内取齐，外部连接在基线固定前后提交的
  修订分别按“两侧均包含 / 两侧均不包含”处理，且外部数据不被回滚。
"""

import sqlite3

from fastapi.testclient import TestClient  # noqa: F401  (fixture 间接使用)

from app import database as db


# --------------------------------------------------------------------------- #
# 测试数据构造辅助
# --------------------------------------------------------------------------- #
def _contract_law_1999(expires_at=None):
    clauses = [{
        "clause_id": "ART-10",
        "title": "第十条",
        "effective_at": "2000-01-01T00:00:00Z",
    }]
    if expires_at:
        clauses[0]["expires_at"] = expires_at
    return {
        "doc_id": "CONTRACT-LAW",
        "version": "1999",
        "name": "合同法",
        "effective_at": "2000-01-01T00:00:00Z",
        "clauses": clauses,
        "revisions": [],
        "citations": [],
    }


def _civil_code_2021(kind="revises"):
    return {
        "doc_id": "CIVIL-CODE",
        "version": "2021",
        "name": "民法典",
        "effective_at": "2021-01-01T00:00:00Z",
        "clauses": [{
            "clause_id": "ART-502",
            "title": "第五百零二条",
            "effective_at": "2021-01-01T00:00:00Z",
        }],
        "revisions": [{
            "kind": kind,
            "predecessor_doc": "CONTRACT-LAW",
            "predecessor_version": "1999",
            "predecessor_clause": "ART-10",
            "successor_doc": "CIVIL-CODE",
            "successor_version": "2021",
            "successor_clause": "ART-502",
            "note": "合同法并入民法典，条号变更",
        }],
        "citations": [],
    }


def _repeal_act_2020():
    """纯废止令（无 successor），2020-06-01 生效。"""
    return {
        "doc_id": "REPEAL-ACT",
        "version": "2020",
        "name": "废止令",
        "effective_at": "2020-06-01T00:00:00Z",
        "clauses": [{
            "clause_id": "ART-1",
            "effective_at": "2020-06-01T00:00:00Z",
        }],
        "revisions": [{
            "kind": "repeals",
            "predecessor_doc": "CONTRACT-LAW",
            "predecessor_version": "1999",
            "predecessor_clause": "ART-10",
            "note": "本法生效之日起废止",
        }],
        "citations": [],
    }


def _citation_doc(doc_id, version, clause, target_doc, target_clause,
                  target_version=None, effective_at="2022-01-01T00:00:00Z"):
    return {
        "doc_id": doc_id,
        "version": version,
        "effective_at": effective_at,
        "clauses": [{
            "clause_id": clause,
            "effective_at": effective_at,
        }],
        "citations": [{
            "source_clause": clause,
            "target_doc": target_doc,
            "target_clause": target_clause,
            "target_version": target_version,
        }],
        "revisions": [],
    }


def _post_corpus(client, payload):
    r = client.post("/corpus", json=payload)
    assert r.status_code == 201, r.text
    return r


# --------------------------------------------------------------------------- #
# 核心：跨文档/跨条号修订改变结论与解析版本
# --------------------------------------------------------------------------- #
def test_rehearse_shows_status_and_resolved_change_across_docs(client):
    _post_corpus(client, _contract_law_1999(expires_at="2021-01-01T00:00:00Z"))
    _post_corpus(client, _citation_doc(
        "JUDICIAL-DOC", "2022", "ART-1", "CONTRACT-LAW", "ART-10"))

    target = "2022-06-01T00:00:00Z"
    # 预演前：旧版已失效、无接续 → repealed
    before = client.post("/audit", json={"target_at": target}).json()
    assert before["results"][0]["status"] == "repealed"

    r = client.post("/rehearse", json={
        "candidate": _civil_code_2021(),
        "target_at": target,
    })
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["total"] == 1
    assert body["changed"] == 1
    item = body["changes"][0]
    assert item["citation_id"] == 1
    assert item["changes"] == ["status", "resolved", "chains"]

    # 两份完整审查结果
    assert item["before"]["status"] == "repealed"
    assert item["before"]["resolved"] is None
    assert item["after"]["status"] == "effective"
    assert item["after"]["resolved"] == {
        "doc_id": "CIVIL-CODE", "version": "2021", "clause_id": "ART-502",
    }
    # 假定登记后的链路含两代节点（跨文档、跨条号）
    nodes = [s["node"] for s in item["after"]["chains"][0]["steps"]]
    assert nodes[0]["version"] == "1999"
    assert nodes[-1] == {
        "doc_id": "CIVIL-CODE", "version": "2021", "clause_id": "ART-502",
    }
    # 预演前链路只有一代
    assert len(item["before"]["chains"][0]["steps"]) == 1


def test_rehearse_chain_only_change(client):
    """状态与解析版本不变，但链路因跨条号接续而变长。"""
    # 旧法长期有效（无 expires_at），目标日仍 effective
    _post_corpus(client, _contract_law_1999())
    _post_corpus(client, _citation_doc(
        "JUDICIAL-DOC", "2022", "ART-1", "CONTRACT-LAW", "ART-10"))

    r = client.post("/rehearse", json={
        "candidate": _civil_code_2021(),
        "target_at": "2010-06-01T00:00:00Z",  # 新版尚未生效，旧版仍有效
    })
    assert r.status_code == 200, r.text
    item = r.json()["changes"][0]
    # 结论仍为 effective、resolved 仍指向 1999 版
    assert "status" not in item["changes"]
    assert "resolved" not in item["changes"]
    assert item["changes"] == ["chains"]
    assert item["after"]["chains"][0]["steps"][-1][
        "state_at_target"
    ] == "not_yet_effective"


def test_rehearse_no_change_returns_empty(client):
    _post_corpus(client, _contract_law_1999())
    _post_corpus(client, _citation_doc(
        "JUDICIAL-DOC", "2022", "ART-1", "CONTRACT-LAW", "ART-10"))

    # 预演一个与被引条文毫无关系的文档版本
    r = client.post("/rehearse", json={
        "candidate": {
            "doc_id": "UNRELATED",
            "version": "2030",
            "effective_at": "2030-01-01T00:00:00Z",
            "clauses": [{
                "clause_id": "ART-1",
                "effective_at": "2030-01-01T00:00:00Z",
            }],
        },
        "target_at": "2022-06-01T00:00:00Z",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["changed"] == 0
    assert body["changes"] == []


# --------------------------------------------------------------------------- #
# 纯废止 / 固定引用沿用 /audit 判定
# --------------------------------------------------------------------------- #
def test_rehearse_terminal_repeal_changes_verdict(client):
    _post_corpus(client, _contract_law_1999())
    _post_corpus(client, _citation_doc(
        "JUDICIAL-DOC", "2022", "ART-1", "CONTRACT-LAW", "ART-10"))

    r = client.post("/rehearse", json={
        "candidate": _repeal_act_2020(),
        "target_at": "2021-01-01T00:00:00Z",
    })
    body = r.json()
    item = body["changes"][0]
    assert item["before"]["status"] == "effective"
    assert item["after"]["status"] == "repealed"
    term = item["after"]["chains"][0]["steps"][-1]
    assert term["terminal"] is True
    assert term["edge_in"] == "repeals"
    assert term["state_at_target"] == "repealed"
    assert term["recorded_by"] == {"doc_id": "REPEAL-ACT", "version": "2020"}

    # 废止时刻之前：结论不变（effective），但链路新增尚未生效的终止步 →
    # 属于“链路发生变化”，仍须列出
    r2 = client.post("/rehearse", json={
        "candidate": _repeal_act_2020(),
        "target_at": "2020-01-01T00:00:00Z",
    })
    body2 = r2.json()
    assert body2["changed"] == 1
    item2 = body2["changes"][0]
    assert item2["changes"] == ["chains"]
    assert item2["before"]["status"] == "effective"
    assert item2["after"]["status"] == "effective"
    term = item2["after"]["chains"][0]["steps"][-1]
    assert term["terminal"] is True
    assert term["state_at_target"] == "not_yet_effective"


def test_rehearse_pinned_citation_uses_audit_rules(client):
    _post_corpus(client, _contract_law_1999())
    _post_corpus(client, _citation_doc(
        "JUDICIAL-DOC", "2022", "ART-1",
        "CONTRACT-LAW", "ART-10", target_version="1999"))

    # 固定引用：纯废止到达时刻后同样不再有效
    r = client.post("/rehearse", json={
        "candidate": _repeal_act_2020(),
        "target_at": "2021-01-01T00:00:00Z",
    })
    item = r.json()["changes"][0]
    assert item["before"]["pinned"] is True
    assert item["before"]["status"] == "effective"
    assert item["after"]["pinned"] is True
    assert item["after"]["status"] == "repealed"


# --------------------------------------------------------------------------- #
# 拟登记版本自带引用不纳入比较
# --------------------------------------------------------------------------- #
def test_rehearse_excludes_candidate_own_citations(client):
    _post_corpus(client, _contract_law_1999())

    # 拟登记的民法典自带一条引用；库中没有任何存量引用
    candidate = _civil_code_2021()
    candidate["citations"] = [{
        "source_clause": "ART-502",
        "target_doc": "CONTRACT-LAW",
        "target_clause": "ART-10",
    }]
    r = client.post("/rehearse", json={
        "candidate": candidate,
        "target_at": "2022-06-01T00:00:00Z",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 0
    assert body["changes"] == []
    # 但拟登记计数仍提示该引用将被写入
    assert body["projected_counts"]["citations"] == 1


# --------------------------------------------------------------------------- #
# 变化按原引用 ID 升序；过滤生效
# --------------------------------------------------------------------------- #
def test_rehearse_changes_sorted_by_citation_id_and_filter(client):
    _post_corpus(client, _contract_law_1999())
    _post_corpus(client, _citation_doc(
        "DOC-A", "2022", "A1", "CONTRACT-LAW", "ART-10"))   # citation id 1
    _post_corpus(client, _citation_doc(
        "DOC-B", "2022", "B1", "CONTRACT-LAW", "ART-10"))   # citation id 2

    r = client.post("/rehearse", json={
        "candidate": _civil_code_2021(),
        "target_at": "2022-06-01T00:00:00Z",
    })
    ids = [c["citation_id"] for c in r.json()["changes"]]
    assert ids == sorted(ids) == [1, 2]

    # 过滤：仅比较 DOC-A 发出的引用
    r = client.post("/rehearse", json={
        "candidate": _civil_code_2021(),
        "target_at": "2022-06-01T00:00:00Z",
        "source_doc": "DOC-A",
    })
    body = r.json()
    assert body["total"] == 1
    assert body["changes"][0]["citation_id"] == 1


# --------------------------------------------------------------------------- #
# 预演不留痕：成功或失败之后仍可正常登记同一份版本
# --------------------------------------------------------------------------- #
def test_rehearse_success_leaves_no_trace_and_can_register_after(client):
    _post_corpus(client, _contract_law_1999(expires_at="2021-01-01T00:00:00Z"))
    _post_corpus(client, _citation_doc(
        "JUDICIAL-DOC", "2022", "ART-1", "CONTRACT-LAW", "ART-10"))

    payload = _civil_code_2021()
    r = client.post("/rehearse", json={
        "candidate": payload,
        "target_at": "2022-06-01T00:00:00Z",
    })
    assert r.status_code == 200

    # 库中查不到拟登记的文档版本
    assert client.get("/corpus/CIVIL-CODE/2021").status_code == 404
    # /audit 结果与预演前一致（旧版已失效且修订关系未落入库 → 仍 repealed）
    audit = client.post("/audit", json={
        "target_at": "2022-06-01T00:00:00Z",
    }).json()
    assert audit["results"][0]["status"] == "repealed"

    # 同一份版本仍可正常登记
    r = client.post("/corpus", json=payload)
    assert r.status_code == 201, r.text
    assert client.get("/corpus/CIVIL-CODE/2021").status_code == 200
    # 登记后再次预演同一版本 → 重复版本 409（沿用 /corpus）
    r = client.post("/rehearse", json={
        "candidate": payload,
        "target_at": "2022-06-01T00:00:00Z",
    })
    assert r.status_code == 409
    assert "不得重复写入" in r.json()["detail"]


def test_rehearse_failure_leaves_no_trace_and_can_register_after(client):
    _post_corpus(client, _contract_law_1999())

    # 非法关系：revises 缺少 successor（与 /corpus 同样判 422）
    bad = _civil_code_2021()
    bad["revisions"][0] = {
        "kind": "revises",
        "predecessor_doc": "CONTRACT-LAW",
        "predecessor_version": "1999",
        "predecessor_clause": "ART-10",
    }
    r = client.post("/rehearse", json={
        "candidate": bad,
        "target_at": "2022-06-01T00:00:00Z",
    })
    assert r.status_code == 422

    # 失败也不留任何痕迹：修正后可立即正式登记
    assert client.get("/corpus/CIVIL-CODE/2021").status_code == 404
    r = client.post("/corpus", json=_civil_code_2021())
    assert r.status_code == 201, r.text

    # 引用源条文不存在等非法载荷同样 422 且不留痕
    r = client.post("/rehearse", json={
        "candidate": {
            "doc_id": "BAD-DOC",
            "version": "2022",
            "effective_at": "2022-01-01T00:00:00Z",
            "clauses": [{"clause_id": "ART-1",
                         "effective_at": "2022-01-01T00:00:00Z"}],
            "citations": [{
                "source_clause": "MISSING",
                "target_doc": "CONTRACT-LAW",
                "target_clause": "ART-10",
            }],
        },
        "target_at": "2022-06-01T00:00:00Z",
    })
    assert r.status_code == 422
    assert client.get("/corpus/BAD-DOC/2022").status_code == 404


def test_rehearse_invalid_candidate_shape_rejected(client):
    # 请求体结构错误（缺少 candidate）走 Pydantic 422
    r = client.post("/rehearse", json={"target_at": "2022-01-01"})
    assert r.status_code == 422
    # candidate 内部额外字段同样拒绝（与 /corpus 一致 extra=forbid）
    r = client.post("/rehearse", json={
        "candidate": {
            "doc_id": "X", "version": "1", "bogus": 1,
        },
    })
    assert r.status_code == 422


def test_rehearse_repeats_are_idempotent_and_isolated(client):
    """连续多次预演互不影响（前一次回滚不污染后一次）。"""
    _post_corpus(client, _contract_law_1999())
    _post_corpus(client, _citation_doc(
        "JUDICIAL-DOC", "2022", "ART-1", "CONTRACT-LAW", "ART-10"))

    payload = _civil_code_2021()
    for _ in range(3):
        r = client.post("/rehearse", json={
            "candidate": payload,
            "target_at": "2022-06-01T00:00:00Z",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["changed"] == 1


# --------------------------------------------------------------------------- #
# 并发登记：另一 SQLite 连接在预演基线确定前后穿插提交
# --------------------------------------------------------------------------- #
def _external_commit(payload):
    """用独立连接（模拟另一个容器/进程）正式登记一份版本并提交。"""
    ext = sqlite3.connect(db.get_db_path())
    try:
        ext.execute("PRAGMA journal_mode = WAL")
        ext.execute("PRAGMA foreign_keys = ON")
        with ext:
            db.insert_corpus(ext, db.CorpusIn.model_validate(payload))
    finally:
        ext.close()


def test_rehearse_external_commit_after_baseline_excluded_from_both_sides(
        client, monkeypatch):
    """基线读取完成后另一连接提交的修订，不得混入 before/after 任一侧。

    在基线最后一次读取（list_edges）返回后、草案叠加之前，通过另一连接
    提交民法典 2021（含一条自带引用）。预演自身叠加的草案是一个无关文档，
    因此若基线未固定：外部修订会把存量引用从 repealed 变成 effective，
    该变化被错误归到无关草案上（changed 虚增），外部自带引用也可能串入。
    """
    _post_corpus(client, _contract_law_1999(expires_at="2021-01-01T00:00:00Z"))
    _post_corpus(client, _citation_doc(
        "JUDICIAL-DOC", "2022", "ART-1", "CONTRACT-LAW", "ART-10"))

    external = _civil_code_2021()
    external["citations"] = [{
        "source_clause": "ART-502",
        "target_doc": "CONTRACT-LAW",
        "target_clause": "ART-10",
    }]
    fired = {"done": False}
    real_list_edges = db.list_edges

    def spying_list_edges(conn, *args, **kwargs):
        rows = real_list_edges(conn, *args, **kwargs)
        # 仅在服务共享连接的基线读取完成后触发（叠加层与外部连接不触发）
        if not fired["done"] and conn is client.app.state.conn:
            fired["done"] = True
            _external_commit(external)
        return rows

    monkeypatch.setattr(db, "list_edges", spying_list_edges)

    unrelated = {
        "doc_id": "UNRELATED",
        "version": "2030",
        "effective_at": "2030-01-01T00:00:00Z",
        "clauses": [{
            "clause_id": "ART-1",
            "effective_at": "2030-01-01T00:00:00Z",
        }],
    }
    r = client.post("/rehearse", json={
        "candidate": unrelated,
        "target_at": "2022-06-01T00:00:00Z",
    })
    assert fired["done"], "测试钩子未触发，未模拟到穿插提交"
    assert r.status_code == 200, r.text
    body = r.json()

    # 外部修订在基线固定后提交：存量引用集合固定为原 1 条，
    # 外部修订引起的结论变化不得归到无关草案
    assert body["total"] == 1
    assert body["changed"] == 0
    assert body["changes"] == []
    assert body["projected_counts"]["clauses"] == 1

    # 草案不落库；外部连接已提交的数据也没有被回滚
    assert client.get("/corpus/UNRELATED/2030").status_code == 404
    assert client.get("/corpus/CIVIL-CODE/2021").status_code == 200
    audit = client.post("/audit", json={
        "target_at": "2022-06-01T00:00:00Z",
    }).json()
    statuses = {x["citation_id"]: x["status"] for x in audit["results"]}
    assert statuses == {1: "effective", 2: "effective"}

    # 随后仍能正常登记合法草案
    r = client.post("/corpus", json=unrelated)
    assert r.status_code == 201, r.text


def test_rehearse_external_commit_before_baseline_present_on_both_sides(
        client, monkeypatch):
    """基线固定前已提交的外部修订，应同时出现在 before/after 两侧。"""
    _post_corpus(client, _contract_law_1999(expires_at="2021-01-01T00:00:00Z"))
    _post_corpus(client, _citation_doc(
        "JUDICIAL-DOC", "2022", "ART-1", "CONTRACT-LAW", "ART-10"))

    fired = {"done": False}
    real_document_version_exists = db.document_version_exists

    def spying_exists(conn, *args, **kwargs):
        if not fired["done"] and conn is client.app.state.conn:
            fired["done"] = True
            # 在版本占用检查之前（基线内最早时刻）完成提交
            _external_commit(_civil_code_2021())
        return real_document_version_exists(conn, *args, **kwargs)

    monkeypatch.setattr(db, "document_version_exists", spying_exists)

    r = client.post("/rehearse", json={
        "candidate": {
            "doc_id": "UNRELATED",
            "version": "2030",
            "effective_at": "2030-01-01T00:00:00Z",
            "clauses": [{
                "clause_id": "ART-1",
                "effective_at": "2030-01-01T00:00:00Z",
            }],
        },
        "target_at": "2022-06-01T00:00:00Z",
    })
    assert fired["done"]
    assert r.status_code == 200, r.text
    body = r.json()

    # 外部民法典已属于基线：存量引用两侧都解析到 CIVIL-CODE@2021，
    # 草案与之无关，故无变化
    assert body["total"] == 1
    assert body["changed"] == 0
    audit = client.post("/audit", json={
        "target_at": "2022-06-01T00:00:00Z",
    }).json()
    assert audit["results"][0]["status"] == "effective"
    assert audit["results"][0]["resolved"] == {
        "doc_id": "CIVIL-CODE", "version": "2021", "clause_id": "ART-502",
    }
    assert client.get("/corpus/CIVIL-CODE/2021").status_code == 200


def test_rehearse_422_under_interleaved_external_commit_leaves_both_intact(
        client, monkeypatch):
    """基线读取期间外部提交 + 草案非法关系 422：草案不落库，外部数据不回滚。"""
    _post_corpus(client, _contract_law_1999())

    fired = {"done": False}
    real_list_edges = db.list_edges

    def spying_list_edges(conn, *args, **kwargs):
        rows = real_list_edges(conn, *args, **kwargs)
        if not fired["done"] and conn is client.app.state.conn:
            fired["done"] = True
            _external_commit(_repeal_act_2020())
        return rows

    monkeypatch.setattr(db, "list_edges", spying_list_edges)

    bad = _civil_code_2021()
    bad["revisions"][0] = {
        "kind": "revises",
        "predecessor_doc": "CONTRACT-LAW",
        "predecessor_version": "1999",
        "predecessor_clause": "ART-10",
    }
    r = client.post("/rehearse", json={
        "candidate": bad,
        "target_at": "2022-06-01T00:00:00Z",
    })
    assert fired["done"]
    assert r.status_code == 422

    # 非法草案不留痕
    assert client.get("/corpus/CIVIL-CODE/2021").status_code == 404
    # 穿插期间由另一连接提交的废止令依然在库
    assert client.get("/corpus/REPEAL-ACT/2020").status_code == 200

    # 修正载荷后仍可正常登记
    r = client.post("/corpus", json=_civil_code_2021())
    assert r.status_code == 201, r.text


def test_rehearse_409_is_judged_against_pinned_baseline(client, monkeypatch):
    """基线固定后另一连接抢先登记同一版本：预演仍按基线快照正常给出。

    快照语义下预演不感知基线之后的提交；预演不落库。随后直接登记时
    由 /corpus 依据最新库状态返回 409（外部连接的数据同样保留）。
    """
    _post_corpus(client, _contract_law_1999(expires_at="2021-01-01T00:00:00Z"))
    _post_corpus(client, _citation_doc(
        "JUDICIAL-DOC", "2022", "ART-1", "CONTRACT-LAW", "ART-10"))

    fired = {"done": False}
    real_list_edges = db.list_edges

    def spying_list_edges(conn, *args, **kwargs):
        rows = real_list_edges(conn, *args, **kwargs)
        if not fired["done"] and conn is client.app.state.conn:
            fired["done"] = True
            _external_commit(_civil_code_2021())
        return rows

    monkeypatch.setattr(db, "list_edges", spying_list_edges)

    # 与外部提交完全相同的版本；快照在占用检查时尚不存在该版本
    r = client.post("/rehearse", json={
        "candidate": _civil_code_2021(),
        "target_at": "2022-06-01T00:00:00Z",
    })
    assert fired["done"]
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidate"] == {"doc_id": "CIVIL-CODE", "version": "2021"}
    assert body["changed"] == 1  # 草案效果只与固定基线比较，不与外部提交相消

    # 外部提交保留；此时再以同一版本登记 → /corpus 返回 409
    assert client.get("/corpus/CIVIL-CODE/2021").status_code == 200
    r = client.post("/corpus", json=_civil_code_2021())
    assert r.status_code == 409
