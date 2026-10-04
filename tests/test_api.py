"""端到端测试：写入 -> 审查 的全部分类路径。"""

import pytest


# --------------------------------------------------------------------------- #
# 测试数据构造辅助
# --------------------------------------------------------------------------- #
def v1_payload(tz="Z"):
    """合同法 1999 初版：第10条，2000-01-01 生效。"""
    return {
        "doc_id": "CONTRACT-LAW",
        "version": "1999",
        "name": "合同法",
        "effective_at": f"2000-01-01T00:00:00{tz}",
        "clauses": [
            {
                "clause_id": "ART-10",
                "title": "第十条",
                "text": "1999 版第十条原文",
                "effective_at": f"2000-01-01T00:00:00{tz}",
                "expires_at": f"2021-01-01T00:00:00{tz}",
            }
        ],
        "citations": [],
        "revisions": [],
    }


def v2_payload():
    """民法典 2021：第五百零二条 修订并接续 合同法第十条。"""
    return {
        "doc_id": "CIVIL-CODE",
        "version": "2021",
        "name": "民法典",
        "effective_at": "2021-01-01T00:00:00Z",
        "clauses": [
            {
                "clause_id": "ART-502",
                "title": "第五百零二条",
                "text": "2021 版第五百零二条",
                "effective_at": "2021-01-01T00:00:00Z",
            }
        ],
        "revisions": [
            {
                "kind": "revises",
                "predecessor_doc": "CONTRACT-LAW",
                "predecessor_version": "1999",
                "predecessor_clause": "ART-10",
                "successor_doc": "CIVIL-CODE",
                "successor_version": "2021",
                "successor_clause": "ART-502",
                "note": "合同法并入民法典",
            }
        ],
        "citations": [],
    }


# --------------------------------------------------------------------------- #
# 基础行为
# --------------------------------------------------------------------------- #
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_write_and_get_corpus(client):
    r = client.post("/corpus", json=v1_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["counts"]["clauses"] == 1

    r = client.get("/corpus/CONTRACT-LAW/1999")
    assert r.status_code == 200
    assert r.json()["clauses"][0]["clause_id"] == "ART-10"


def test_duplicate_document_version_rejected(client):
    client.post("/corpus", json=v1_payload())
    r = client.post("/corpus", json=v1_payload())
    assert r.status_code == 409
    assert "不得重复写入" in r.json()["detail"]


def test_timezone_inputs_normalized_to_utc(client):
    payload = v1_payload(tz="+08:00")
    # 2000-01-01T00:00:00+08:00 == 1999-12-31T16:00:00Z
    r = client.post("/corpus", json=payload)
    assert r.status_code == 201, r.text
    doc = client.get("/corpus/CONTRACT-LAW/1999").json()
    assert doc["effective_at"] == "1999-12-31T16:00:00+00:00"


def test_naive_datetime_treated_as_utc(client):
    p = v1_payload()
    p["effective_at"] = "2000-01-01T00:00:00"
    client.post("/corpus", json=p)
    doc = client.get("/corpus/CONTRACT-LAW/1999").json()
    assert doc["effective_at"] == "2000-01-01T00:00:00+00:00"


def test_date_only_target_at(client):
    client.post("/corpus", json=v1_payload())
    client.post("/corpus", json=v2_payload())
    _add_citation(client, "CIVIL-CODE", "2021", "ART-502",
                  target_doc="CONTRACT-LAW", target_clause="ART-10")
    r = client.post("/audit", json={"target_at": "2021-06-01"})
    assert r.status_code == 200
    data = r.json()
    assert data["target_at"] == "2021-06-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# 审查分类
# --------------------------------------------------------------------------- #
def _add_citation(client, doc, version, source, target_doc, target_clause,
                  target_version=None):
    """在某文档版本中补一条引用（独立小批次，仅含引用源条文占位）。

    测试直接写库更方便：用专用接口不存在，这里借助 /corpus 新增“引用文档”。
    """
    payload = {
        "doc_id": doc,
        "version": version + "-refs" if version == "2021" else version + "-r",
        "effective_at": "2000-01-01T00:00:00Z",
        "clauses": [
            {"clause_id": source + "#cite",
             "effective_at": "2000-01-01T00:00:00Z"}
        ],
        "citations": [
            {
                "source_clause": source + "#cite",
                "target_doc": target_doc,
                "target_clause": target_clause,
                "target_version": target_version,
            }
        ],
        "revisions": [],
    }
    r = client.post("/corpus", json=payload)
    assert r.status_code == 201, r.text
    return r


def test_effective_resolves_latest_along_chain(client):
    client.post("/corpus", json=v1_payload())
    client.post("/corpus", json=v2_payload())
    _add_citation(client, "JUDICIAL-DOC", "2022", "ART-1",
                  "CONTRACT-LAW", "ART-10")
    r = client.post("/audit", json={"target_at": "2022-06-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "effective"
    # 沿链解析到民法典版本
    assert result["resolved"]["doc_id"] == "CIVIL-CODE"
    assert result["resolved"]["version"] == "2021"
    # 可追溯链路包含两代
    steps = result["chains"][0]["steps"]
    assert steps[0]["node"]["version"] == "1999"
    assert steps[-1]["node"]["version"] == "2021"
    assert steps[-1]["state_at_target"] == "effective"
    assert steps[1]["edge_in"] == "revises"


def test_repealed_status(client):
    client.post("/corpus", json=v1_payload())
    # 2021 版直接明文废止，无接续
    client.post("/corpus", json={
        "doc_id": "CIVIL-CODE", "version": "2021",
        "effective_at": "2021-01-01T00:00:00Z",
        "clauses": [{"clause_id": "ART-X",
                     "effective_at": "2021-01-01T00:00:00Z"}],
        "revisions": [{
            "kind": "repeals",
            "predecessor_doc": "CONTRACT-LAW",
            "predecessor_version": "1999",
            "predecessor_clause": "ART-10",
        }],
        "citations": [],
    })
    _add_citation(client, "JUDICIAL-DOC", "2022", "ART-1",
                  "CONTRACT-LAW", "ART-10")
    r = client.post("/audit", json={"target_at": "2022-01-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "repealed"
    # 链路末步为明文废止终止点
    assert result["chains"][0]["steps"][-1]["terminal"] is True


def test_not_yet_effective(client):
    client.post("/corpus", json=v1_payload())
    client.post("/corpus", json=v2_payload())
    _add_citation(client, "JUDICIAL-DOC", "1999", "ART-1",
                  "CONTRACT-LAW", "ART-10")
    r = client.post("/audit", json={"target_at": "1999-06-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "not_yet_effective"


def test_pinned_version_kept(client):
    client.post("/corpus", json=v1_payload())
    client.post("/corpus", json=v2_payload())
    _add_citation(client, "JUDICIAL-DOC", "2022", "ART-1",
                  "CONTRACT-LAW", "ART-10", target_version="1999")
    # 固定引用 1999 版，目标日期该版已失效
    r = client.post("/audit", json={"target_at": "2022-01-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["pinned"] is True
    assert result["status"] == "repealed"
    assert result["anchors"][0]["version"] == "1999"


def test_pinned_missing_version_is_broken(client):
    client.post("/corpus", json=v1_payload())
    _add_citation(client, "JUDICIAL-DOC", "2022", "ART-1",
                  "CONTRACT-LAW", "ART-10", target_version="1986")
    r = client.post("/audit", json={"target_at": "2022-01-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "broken_chain"


def test_not_found_target(client):
    client.post("/corpus", json=v1_payload())
    _add_citation(client, "JUDICIAL-DOC", "2022", "ART-1",
                  "GHOST-LAW", "ART-999")
    r = client.post("/audit", json={"target_at": "2022-01-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "not_found"


def test_ambiguous_branch(client):
    """同一旧条文被两个不同文档版本接续 -> 分叉歧义。"""
    client.post("/corpus", json=v1_payload())
    # 分支 A
    client.post("/corpus", json={
        "doc_id": "BRANCH-A", "version": "2021",
        "effective_at": "2021-01-01T00:00:00Z",
        "clauses": [{
            "clause_id": "ART-10",
            "effective_at": "2021-01-01T00:00:00Z",
        }],
        "revisions": [{
            "kind": "revises",
            "predecessor_doc": "CONTRACT-LAW",
            "predecessor_version": "1999",
            "predecessor_clause": "ART-10",
            "successor_doc": "BRANCH-A", "successor_version": "2021",
            "successor_clause": "ART-10",
        }],
        "citations": [],
    })
    # 分支 B
    client.post("/corpus", json={
        "doc_id": "BRANCH-B", "version": "2021",
        "effective_at": "2021-01-01T00:00:00Z",
        "clauses": [{
            "clause_id": "ART-10",
            "effective_at": "2021-01-01T00:00:00Z",
        }],
        "revisions": [{
            "kind": "revises",
            "predecessor_doc": "CONTRACT-LAW",
            "predecessor_version": "1999",
            "predecessor_clause": "ART-10",
            "successor_doc": "BRANCH-B", "successor_version": "2021",
            "successor_clause": "ART-10",
        }],
        "citations": [],
    })
    _add_citation(client, "JUDICIAL-DOC", "2022", "ART-1",
                  "CONTRACT-LAW", "ART-10")
    r = client.post("/audit", json={"target_at": "2022-01-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "ambiguous"
    assert len(result["effective_candidates"]) == 2
    # 两条链路分别可追溯
    assert len(result["chains"]) == 2


def test_circular_chain(client):
    """A -> B -> A 的修订环。"""
    client.post("/corpus", json={
        "doc_id": "CYCLIC", "version": "v1",
        "effective_at": "2020-01-01T00:00:00Z",
        "clauses": [{"clause_id": "C1",
                     "effective_at": "2020-01-01T00:00:00Z"}],
        "citations": [],
        "revisions": [],
    })
    client.post("/corpus", json={
        "doc_id": "CYCLIC", "version": "v2",
        "effective_at": "2021-01-01T00:00:00Z",
        "clauses": [{"clause_id": "C1",
                     "effective_at": "2021-01-01T00:00:00Z"}],
        "revisions": [{
            "kind": "revises",
            "predecessor_doc": "CYCLIC", "predecessor_version": "v1",
            "predecessor_clause": "C1",
            "successor_doc": "CYCLIC", "successor_version": "v2",
            "successor_clause": "C1",
        }],
        "citations": [],
    })
    # 用 v2 批次无法回到已存在的 v1，直接构造“前序为 v2、接续为 v1”的环：
    # 借助第三个批次，其条文为 v3 revises v2，再单独造一条 v1<-v2 不允许，
    # 因此环用直接插入修订边的方式：通过第三文档 X v1 节点指向旧节点。
    client.post("/corpus", json={
        "doc_id": "CYCLIC", "version": "v3",
        "effective_at": "2022-01-01T00:00:00Z",
        "clauses": [{"clause_id": "C1",
                     "effective_at": "2022-01-01T00:00:00Z"}],
        "revisions": [{
            "kind": "revises",
            "predecessor_doc": "CYCLIC", "predecessor_version": "v2",
            "predecessor_clause": "C1",
            "successor_doc": "CYCLIC", "successor_version": "v3",
            "successor_clause": "C1",
        }],
        "citations": [],
    })
    # 直接补一条制造环的边：v3 -> v1（绕过业务校验，模拟脏数据/迁移导入）
    from app.main import app
    conn = app.state.conn
    conn.execute(
        "INSERT INTO revision_edges"
        " (kind, predecessor_doc, predecessor_version, predecessor_clause,"
        "  successor_doc, successor_version, successor_clause,"
        "  recorded_by_doc, recorded_by_version, note)"
        " VALUES ('revises','CYCLIC','v3','C1','CYCLIC','v1','C1',"
        "         'CYCLIC','v3','imported')"
    )
    conn.commit()

    _add_citation(client, "JUDICIAL-DOC", "2023", "ART-1",
                  "CYCLIC", "C1")
    r = client.post("/audit", json={"target_at": "2023-01-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "circular"
    assert result["cycle"] is not None
    assert len(result["cycle"]) >= 3


def test_repeals_with_successor_continues_chain(client):
    """repeals 带接续节点 = 废止并由新法取代：链延续，进入边为 repeals。"""
    client.post("/corpus", json=v1_payload())  # CONTRACT-LAW@1999#ART-10
    # 2021 新法明文废止旧法并取代其条文
    client.post("/corpus", json={
        "doc_id": "CIVIL-CODE", "version": "2021",
        "effective_at": "2021-01-01T00:00:00Z",
        "clauses": [{
            "clause_id": "ART-502",
            "effective_at": "2021-01-01T00:00:00Z",
        }],
        "revisions": [{
            "kind": "repeals",
            "predecessor_doc": "CONTRACT-LAW",
            "predecessor_version": "1999",
            "predecessor_clause": "ART-10",
            "successor_doc": "CIVIL-CODE",
            "successor_version": "2021",
            "successor_clause": "ART-502",
            "note": "明文废止并取代",
        }],
        "citations": [],
    })
    _add_citation(client, "JUDICIAL-DOC", "2022", "ART-1",
                  "CONTRACT-LAW", "ART-10")
    r = client.post("/audit", json={"target_at": "2022-01-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "effective"
    assert result["resolved"]["doc_id"] == "CIVIL-CODE"
    steps = result["chains"][0]["steps"]
    assert len(steps) == 2
    assert steps[0]["state_at_target"] == "repealed"
    assert steps[1]["edge_in"] == "repeals"
    assert steps[1]["state_at_target"] == "effective"
    assert steps[1]["terminal"] is False


def test_summary_counts(client):
    client.post("/corpus", json=v1_payload())
    _add_citation(client, "DOC-X", "2022", "A1", "CONTRACT-LAW", "ART-10")
    _add_citation(client, "DOC-Y", "2022", "A1", "NO-SUCH", "X9")
    r = client.post("/audit", json={"target_at": "2022-01-01T00:00:00Z"})
    data = r.json()
    assert data["total"] == 2
    assert data["summary"]["repealed"] == 1
    assert data["summary"]["not_found"] == 1


# --------------------------------------------------------------------------- #
# 无接续明文废止：以登记版本生效时间为废止时刻
# --------------------------------------------------------------------------- #
def _old_law_without_expiry(client):
    """旧法 2000 版：未填 expires_at（长期有效，直至被明文废止）。"""
    r = client.post("/corpus", json={
        "doc_id": "OLD-LAW", "version": "2000",
        "name": "旧条例",
        "effective_at": "2000-01-01T00:00:00Z",
        "clauses": [{
            "clause_id": "ART-1",
            "title": "第一条",
            "effective_at": "2000-01-01T00:00:00Z",
        }],
        "revisions": [],
        "citations": [],
    })
    assert r.status_code == 201, r.text


def _repeal_act(client, effective_at="2020-06-01T00:00:00Z"):
    """废止令 2020 版：生效时间明确，登记对旧法第一条的纯废止（无接续）。"""
    r = client.post("/corpus", json={
        "doc_id": "REPEAL-ACT", "version": "2020",
        "name": "废止令",
        "effective_at": effective_at,
        "clauses": [{
            "clause_id": "ART-1",
            "effective_at": effective_at,
        }],
        "revisions": [{
            "kind": "repeals",
            "predecessor_doc": "OLD-LAW",
            "predecessor_version": "2000",
            "predecessor_clause": "ART-1",
            "note": "本法生效之日起废止旧条例",
        }],
        "citations": [],
    })
    assert r.status_code == 201, r.text


def test_terminal_repeal_moment_from_recording_version(client):
    """旧条款未填 expires_at：纯废止以登记版本 effective_at 为废止时刻。"""
    _old_law_without_expiry(client)
    _repeal_act(client)  # 2020-06-01 生效的废止令
    _add_citation(client, "JUDICIAL-DOC", "2021", "ART-9", "OLD-LAW", "ART-1")

    # 废止时刻之前：保留旧条款原有判定（生效前 / 有效）
    r = client.post("/audit", json={"target_at": "1999-06-01T00:00:00Z"})
    assert r.json()["results"][0]["status"] == "not_yet_effective"
    r = client.post("/audit", json={"target_at": "2020-05-31T23:59:59Z"})
    result = r.json()["results"][0]
    assert result["status"] == "effective"
    assert result["resolved"]["version"] == "2000"
    # 废止终止步尚未生效，与审查结论一致
    term = result["chains"][0]["steps"][-1]
    assert term["terminal"] is True
    assert term["state_at_target"] == "not_yet_effective"
    assert term["effective_at"] == "2020-06-01T00:00:00+00:00"

    # 恰达废止时刻（左闭右开）：旧条款即视为已废止
    r = client.post("/audit", json={"target_at": "2020-06-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "repealed"
    assert result["resolved"] is None
    steps = result["chains"][0]["steps"]
    assert steps[0]["state_at_target"] == "repealed"
    term = steps[-1]
    assert term["terminal"] is True
    assert term["edge_in"] == "repeals"
    assert term["state_at_target"] == "repealed"
    assert term["effective_at"] == "2020-06-01T00:00:00+00:00"
    assert term["recorded_by"] == {"doc_id": "REPEAL-ACT", "version": "2020"}

    # 废止之后：固定为已废止
    r = client.post("/audit", json={"target_at": "2022-01-01T00:00:00Z"})
    assert r.json()["results"][0]["status"] == "repealed"


def test_terminal_repeal_pinned_version_not_effective_after(client):
    """固定版本引用：达到废止时刻后同样不得列为有效。"""
    _old_law_without_expiry(client)
    _repeal_act(client)
    _add_citation(client, "JUDICIAL-DOC", "2021", "ART-9",
                  "OLD-LAW", "ART-1", target_version="2000")

    r = client.post("/audit", json={"target_at": "2020-01-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["pinned"] is True
    assert result["status"] == "effective"

    r = client.post("/audit", json={"target_at": "2021-01-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["pinned"] is True
    assert result["status"] == "repealed"


def test_terminal_repeal_with_live_successor_resolves_it(client):
    """另有有效接续条款时，非固定引用仍解析到接续版本。"""
    _old_law_without_expiry(client)
    # 新法 2020 版修订接续旧法第一条
    client.post("/corpus", json={
        "doc_id": "NEW-LAW", "version": "2020",
        "effective_at": "2020-06-01T00:00:00Z",
        "clauses": [{
            "clause_id": "ART-1",
            "effective_at": "2020-06-01T00:00:00Z",
        }],
        "revisions": [{
            "kind": "revises",
            "predecessor_doc": "OLD-LAW",
            "predecessor_version": "2000",
            "predecessor_clause": "ART-1",
            "successor_doc": "NEW-LAW",
            "successor_version": "2020",
            "successor_clause": "ART-1",
        }],
        "citations": [],
    })
    _repeal_act(client)  # 同一旧条款另被纯废止登记
    _add_citation(client, "JUDICIAL-DOC", "2021", "ART-9", "OLD-LAW", "ART-1")

    r = client.post("/audit", json={"target_at": "2021-01-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "effective"
    assert result["resolved"]["doc_id"] == "NEW-LAW"
    assert result["resolved"]["version"] == "2020"


def test_terminal_repeal_respects_earlier_own_expiry(client):
    """旧条款自身失效时间更早时，仍按原失效时间判断。"""
    client.post("/corpus", json={
        "doc_id": "OLD-LAW", "version": "2000",
        "effective_at": "2000-01-01T00:00:00Z",
        "clauses": [{
            "clause_id": "ART-1",
            "effective_at": "2000-01-01T00:00:00Z",
            "expires_at": "2019-01-01T00:00:00Z",
        }],
        "revisions": [],
        "citations": [],
    })
    _repeal_act(client)  # 废止时刻 2020-06-01，晚于自身失效时间
    _add_citation(client, "JUDICIAL-DOC", "2021", "ART-9", "OLD-LAW", "ART-1")

    # 自身失效后、废止时刻前：已按原失效时间判定为已废止
    r = client.post("/audit", json={"target_at": "2019-06-01T00:00:00Z"})
    result = r.json()["results"][0]
    assert result["status"] == "repealed"
    assert "明文废止" not in result["reason"]
    # 废止时刻之后亦然
    r = client.post("/audit", json={"target_at": "2021-01-01T00:00:00Z"})
    assert r.json()["results"][0]["status"] == "repealed"
