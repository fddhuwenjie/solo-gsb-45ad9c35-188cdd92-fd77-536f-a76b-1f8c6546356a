"""一键写入演示语料：合同法 1999 -> 民法典 2021（条号变化）的修订链，
外加一条引用。运行方式（服务启动后）：

    python scripts/seed_demo.py
"""

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return {"code": resp.status, "body": json.loads(resp.read())}
    except urllib.error.HTTPError as exc:
        return {"code": exc.code, "body": json.loads(exc.read())}


PAYLOADS = [
    {
        # 带时区输入，服务端统一转 UTC
        "doc_id": "CONTRACT-LAW",
        "version": "1999",
        "name": "中华人民共和国合同法",
        "effective_at": "2000-01-01T00:00:00+08:00",
        "clauses": [
            {
                "clause_id": "ART-10",
                "title": "第十条",
                "text": "当事人订立合同，有书面形式、口头形式和其他形式。",
                "effective_at": "2000-01-01T00:00:00+08:00",
                "expires_at": "2021-01-01T00:00:00Z",
            }
        ],
        "revisions": [],
        "citations": [],
    },
    {
        "doc_id": "CIVIL-CODE",
        "version": "2021",
        "name": "中华人民共和国民法典",
        "effective_at": "2021-01-01T00:00:00Z",
        "clauses": [
            {
                "clause_id": "ART-502",
                "title": "第五百零二条",
                "text": "依法成立的合同，自成立时生效……",
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
                "note": "合同法废止，条文并入民法典（条号变更）",
            }
        ],
        "citations": [],
    },
    {
        "doc_id": "SPC-INTERP",
        "version": "2022",
        "name": "最高法合同编通则解释",
        "effective_at": "2022-01-01T00:00:00Z",
        "clauses": [
            {
                "clause_id": "ART-1",
                "title": "第一条",
                "effective_at": "2022-01-01T00:00:00Z",
            }
        ],
        "revisions": [],
        "citations": [
            {
                "source_clause": "ART-1",
                "target_doc": "CONTRACT-LAW",
                "target_clause": "ART-10",
                "context": "依据原合同法相关规定……",
            }
        ],
    },
]


def main() -> None:
    for p in PAYLOADS:
        res = post("/corpus", p)
        print(res["code"], p["doc_id"], p["version"], res["body"])
    print("\n审查 2022-06-01：")
    audit = post("/audit", {"target_at": "2022-06-01"})
    for r in audit["body"]["results"]:
        print(" ", r["status_label"], "->", r["resolved"])


if __name__ == "__main__":
    main()
