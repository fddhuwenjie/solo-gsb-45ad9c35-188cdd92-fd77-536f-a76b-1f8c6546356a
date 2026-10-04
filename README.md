# 法规修订链引用审查 API

按**目标日期**沿可分支的法规修订链解析条款引用，自动区分引用在该日是
**当日有效 / 已废止 / 尚未生效 / 歧义（链分叉）/ 循环**，并为每条引用返回
从链根到各终止点的**完整可追溯链路**。

技术栈：Python 3.12 · FastAPI · Pydantic v2 · SQLite（WAL）· Uvicorn。

---

## 一、单命令启动

```bash
docker compose up --build
```

- API：http://localhost:8000
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

数据持久化在命名卷 `regulation-data`（容器内 `/data/regulations.db`）。

写入演示数据（另开一个终端）：

```bash
python scripts/seed_demo.py                 # 默认请求 http://127.0.0.1:8000
```

本地裸机运行（无 Docker）：

```bash
pip install -r requirements.txt
DB_PATH=./data/regulations.db uvicorn app.main:app --reload
```

---

## 二、数据模型

| 概念 | 说明 |
| --- | --- |
| 文档版本 `document_versions` | `(doc_id, version)` 唯一；写入的幂等单位 |
| 条款版本 `clause_versions` | 节点 `(doc_id, version, clause_id)`，含 `effective_at` / `expires_at` |
| 修订关系 `revision_edges` | `revises`（修订后接续）或 `repeals`（明文废止；带接续节点表示“废止并由新法取代”，链延续；无接续节点为链终止，并以登记版本的生效时间为废止时刻） |
| 引用 `citations` | 源条文 → 目标条文；`target_version` 缺省为“非固定引用” |

关键语义：

- **条号允许变化**。条文身份沿 `revises` 边传播（如「合同法 1999 第十条」
  →「民法典 2021 第五百零二条」），不依赖条号或文档标识相同。
- **可分支**。同一旧版本被两个新版本接续即形成分叉；目标日有多个同时有效
  的候选时判定为 `ambiguous`，并为每个分叉返回独立链路。
- **生效窗口**采用左闭右开 `[effective_at, expires_at)`；条款未单独给
  生效时间时继承所属文档版本的生效时间。
- **无接续的纯废止**以登记该废止的文档版本的 `effective_at` 为废止时刻
  （UTC、左闭右开）：该时刻之前旧条款按自身窗口判定，到达该时刻后固定
  与非固定引用均不再将其列为有效；旧条款自身 `expires_at` 更早时仍按
  其自身失效时间判定。链路末步（`terminal=true`）的 `effective_at`
  即为该废止时刻。
- **时间一律 UTC**。带时区输入转换为 UTC；无时区输入假定为 UTC；
  仅日期（`YYYY-MM-DD`）按 UTC 00:00:00 处理。
- **幂等写入**。相同 `(doc_id, version)` 重复提交返回 `409 Conflict`；
  修订须使用新版本号。
- 修订关系的前序节点允许暂不在库中（先登记、后补齐），响应以
  `warnings` 提示悬空引用。

### 审查结论

| status | 含义 |
| --- | --- |
| `effective` | 目标日存在唯一有效版本，`resolved` 给出该版本 |
| `repealed` | 最新版本已过失效时间、存在生效空档、或被 `repeals` 明文废止 |
| `not_yet_effective` | 链上版本最早生效时间晚于目标日 |
| `ambiguous` | 链分叉 / 生效窗口重叠，目标日有效候选不唯一 |
| `circular` | `revises` 前向子图存在环，响应给出 `cycle` 节点序列 |
| `not_found` | 被引文档条文在库中不存在任何版本 |
| `broken_chain` | 固定版本不存在，或锚点与修订关系不连通 |

---

## 三、API

### `POST /corpus` —— 写入文档版本

```json
{
  "doc_id": "CIVIL-CODE",
  "version": "2021",
  "name": "中华人民共和国民法典",
  "effective_at": "2021-01-01T00:00:00Z",
  "clauses": [
    {
      "clause_id": "ART-502",
      "title": "第五百零二条",
      "text": "依法成立的合同……",
      "effective_at": "2021-01-01T00:00:00Z"
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
      "note": "并入民法典，条号变更"
    }
  ],
  "citations": [
    {
      "source_clause": "ART-502",
      "target_doc": "PROPERTY-LAW",
      "target_clause": "ART-9",
      "target_version": "2007",
      "context": "引用语境（可选）"
    }
  ]
}
```

- `revisions[].kind = "repeals"` 时 successor 可整体省略，表示无接续的纯废止。
- 条款可省略 `effective_at`（继承文档版本）；`expires_at` 可省略。
- 响应 `201` 返回各类计数与 `warnings`；重复写入 `409`。

### `POST /audit` —— 引用审查

请求体全部字段可选；`target_at` 缺省为**当前 UTC 时间**。可用
`source_doc / source_version / source_clause / target_doc` 过滤范围。

```json
{ "target_at": "2022-06-01T00:00:00Z", "target_doc": "CONTRACT-LAW" }
```

响应（节选）：

```json
{
  "target_at": "2022-06-01T00:00:00+00:00",
  "total": 1,
  "summary": {"effective": 1},
  "results": [
    {
      "status": "effective",
      "status_label": "当日有效",
      "reason": "目标日期存在有效版本",
      "resolved": {"doc_id": "CIVIL-CODE", "version": "2021", "clause_id": "ART-502"},
      "effective_candidates": [ { "...": "..." } ],
      "cycle": null,
      "anchors": [ { "...": "..." } ],
      "chains": [
        {
          "anchor": {"doc_id": "CONTRACT-LAW", "version": "1999", "clause_id": "ART-10"},
          "branch_index": 0,
          "steps": [
            {"node": {"doc_id": "CONTRACT-LAW", "version": "1999", "clause_id": "ART-10"},
             "state_at_target": "repealed", "edge_in": null, "terminal": false},
            {"node": {"doc_id": "CIVIL-CODE", "version": "2021", "clause_id": "ART-502"},
             "state_at_target": "effective", "edge_in": "revises",
             "note": "并入民法典，条号变更", "terminal": false}
          ]
        }
      ]
    }
  ]
}
```

`chains[].steps` 即可追溯链路：每个节点带目标日状态 `state_at_target`
（`effective / repealed / not_yet_effective`）、进入边 `edge_in`
（`revises / repeals`）、修订说明 `note`；链路末步 `terminal=true`
表示 `repeals` 终止点，其 `effective_at` 为废止生效时刻（登记版本的
生效时间），目标日未达到该时刻时终止步状态为 `not_yet_effective`。
歧义时每个分叉一条 chain。

### `POST /rehearse` —— 登记前预演（比较假定登记前后的审查结果）

正式登记一份修订版本**之前**，按与 `POST /corpus` 完全相同的载荷提交
拟登记版本（`candidate`）与预演目标日期 `target_at`，服务对库中
**原有引用**分别在「登记前」与「假定该版本登记后」各做一次 `/audit`
审查，并只返回**结论（status）、解析版本（resolved）或链路（chains）
发生变化**的引用，按原引用 ID 升序列出，每项含 `before` / `after`
两份与 `/audit` 同构的**完整审查结果**。除 `candidate` 与 `target_at`
外，也接受与 `/audit` 相同的 `source_doc / source_version /
source_clause / target_doc` 过滤参数。

语义约定：

- **拟登记版本自带的引用不纳入比较**（`total` 只统计库中原有的存量
  引用）；拟登记版本若含自带引用，只体现在 `projected_counts` 中。
- 修订关系可**跨文档、跨条号**影响旧引用；固定版本引用、链分叉歧义、
  纯废止时刻等判定**完全沿用 `/audit`**（前后两次审查走同一解析逻辑）。
- 除草案（拟登记版本）外，前后两次审查基于**同一份库状态**：引用集合
  固定为登记前的存量引用，仅条款与修订关系叠加拟登记版本。
- **预演不留痕**：无论成功还是失败（含 409/422），都不会写入任何文档、
  条款、修订关系或引用；预演之后仍可用同一份载荷正常 `POST /corpus`
  登记。重复版本返回与 `/corpus` 相同的 `409`，非法关系/非法载荷返回
  相同的 `422` 错误响应。
- 响应另含 `projected_counts` / `projected_warnings`，即该版本若正式
  登记时 `/corpus` 会返回的写入计数与告警（含悬空前序节点提示）。

响应（节选）：

```json
{
  "candidate": {"doc_id": "CIVIL-CODE", "version": "2021"},
  "target_at": "2022-06-01T00:00:00+00:00",
  "total": 1,
  "changed": 1,
  "changes": [
    {
      "citation_id": 1,
      "changes": ["status", "resolved", "chains"],
      "before": { "…与 /audit 的 results[] 完全同构的完整审查结果…" },
      "after":  { "…同上：假定登记后的完整审查结果…" }
    }
  ],
  "projected_counts": {"clauses": 1, "revisions": 1, "citations": 0},
  "projected_warnings": []
}
```

`changes[].changes` 标识具体变化项：`status`（审查结论变化）、
`resolved`（解析到的版本变化）、`chains`（可追溯链路变化，如新增
跨条号接续步、废止终止步或分叉）；无任何引用变化时 `changes` 为 `[]`。

### 可执行示例：预演修订登记，确认影响后再正式登记

以下命令假设服务已在 `http://localhost:8000` 运行
（`docker compose up --build`），并已登记一条链：合同法 1999
第十条（`expires_at = 2021-01-01`）与一条指向它的存量引用
（例如参照上文方式登记 `JUDICIAL-DOC@2022`）。

```bash
# 1) 预演：拟登记民法典 2021（修订接续合同法 1999 第十条，条号变更），
#    目标日 2022-06-01 —— 库中原有引用登记前为 repealed，
#    假定登记后将变为 effective 并解析到民法典 ART-502
curl -s -X POST http://localhost:8000/rehearse \
  -H 'Content-Type: application/json' -d '{
    "target_at": "2022-06-01T00:00:00Z",
    "candidate": {
      "doc_id": "CIVIL-CODE", "version": "2021", "name": "民法典",
      "effective_at": "2021-01-01T00:00:00Z",
      "clauses": [{"clause_id": "ART-502", "title": "第五百零二条",
                   "effective_at": "2021-01-01T00:00:00Z"}],
      "revisions": [{
        "kind": "revises",
        "predecessor_doc": "CONTRACT-LAW", "predecessor_version": "1999",
        "predecessor_clause": "ART-10",
        "successor_doc": "CIVIL-CODE", "successor_version": "2021",
        "successor_clause": "ART-502",
        "note": "并入民法典，条号变更"
      }]
    }
  }'
# => "changed": 1；changes[0].before.status = "repealed"，
#    changes[0].after.status  = "effective"，
#    after.resolved = CIVIL-CODE@2021#ART-502（跨文档、跨条号）

# 2) 预演不落库：拟登记版本尚不存在，/audit 结果仍是登记前状态
curl -s -o /dev/null -w '%{http_code}\n' \
  http://localhost:8000/corpus/CIVIL-CODE/2021   # => 404

# 3) 确认影响可接受后，用同一份载荷正式登记
curl -s -X POST http://localhost:8000/corpus \
  -H 'Content-Type: application/json' -d '{
    "doc_id": "CIVIL-CODE", "version": "2021", "name": "民法典",
    "effective_at": "2021-01-01T00:00:00Z",
    "clauses": [{"clause_id": "ART-502", "title": "第五百零二条",
                 "effective_at": "2021-01-01T00:00:00Z"}],
    "revisions": [{
      "kind": "revises",
      "predecessor_doc": "CONTRACT-LAW", "predecessor_version": "1999",
      "predecessor_clause": "ART-10",
      "successor_doc": "CIVIL-CODE", "successor_version": "2021",
      "successor_clause": "ART-502",
      "note": "并入民法典，条号变更"
    }]
  }'
# => 201 {"created": true, ...}

# 4) 登记后再预演同一版本 → 与 /corpus 相同的 409
curl -s -X POST http://localhost:8000/rehearse \
  -H 'Content-Type: application/json' -d '{
    "target_at": "2022-06-01",
    "candidate": {"doc_id": "CIVIL-CODE", "version": "2021"}
  }'
# => 409 文档版本 CIVIL-CODE@2021 已写入……
```

预演失败同样不留痕：例如 `revisions` 中 `revises` 关系缺少 successor
会返回与 `/corpus` 一致的 `422`，修正载荷后无需任何清理即可重试预演
或直接登记。

### 可执行示例：登记纯废止并做废止前后审查

以下命令假设服务已在 `http://localhost:8000` 运行
（`docker compose up --build`）。

```bash
# 1) 登记旧法：未填 expires_at（长期有效，直至被明文废止）
curl -s -X POST http://localhost:8000/corpus \
  -H 'Content-Type: application/json' -d '{
    "doc_id": "OLD-LAW", "version": "2000", "name": "旧条例",
    "effective_at": "2000-01-01T00:00:00Z",
    "clauses": [{"clause_id": "ART-1", "title": "第一条",
                 "effective_at": "2000-01-01T00:00:00Z"}]
  }'

# 2) 登记一份引用旧法的司法文件
curl -s -X POST http://localhost:8000/corpus \
  -H 'Content-Type: application/json' -d '{
    "doc_id": "JUDICIAL-DOC", "version": "2021",
    "effective_at": "2021-01-01T00:00:00Z",
    "clauses": [{"clause_id": "ART-9",
                 "effective_at": "2021-01-01T00:00:00Z"}],
    "citations": [{"source_clause": "ART-9",
                   "target_doc": "OLD-LAW", "target_clause": "ART-1"}]
  }'

# 3) 登记废止令：2020-06-01 生效，repeals 不带 successor（纯废止）
curl -s -X POST http://localhost:8000/corpus \
  -H 'Content-Type: application/json' -d '{
    "doc_id": "REPEAL-ACT", "version": "2020", "name": "废止令",
    "effective_at": "2020-06-01T00:00:00Z",
    "clauses": [{"clause_id": "ART-1",
                 "effective_at": "2020-06-01T00:00:00Z"}],
    "revisions": [{"kind": "repeals",
                   "predecessor_doc": "OLD-LAW",
                   "predecessor_version": "2000",
                   "predecessor_clause": "ART-1",
                   "note": "本法生效之日起废止旧条例"}]
  }'

# 4) 废止时刻之前审查：旧条款仍有效（resolved 指向 2000 版）
curl -s -X POST http://localhost:8000/audit \
  -H 'Content-Type: application/json' \
  -d '{"target_at": "2020-05-31T00:00:00Z"}'
# => "status": "effective"

# 5) 到达废止时刻（登记版本生效时间，左闭右开）后审查：已废止
curl -s -X POST http://localhost:8000/audit \
  -H 'Content-Type: application/json' \
  -d '{"target_at": "2020-06-01T00:00:00Z"}'
# => "status": "repealed"；chains 末步 terminal=true，
#    effective_at = "2020-06-01T00:00:00+00:00"
```

### 其它端点

- `GET /corpus/{doc_id}/{version}` —— 查看已写入的文档版本（条款/修订/引用）
- `GET /health` —— 健康检查
- `GET /docs` / `/openapi.json` —— 交互式文档与 OpenAPI 模式（`/rehearse` 标记为 `rehearse` 分组）

---

## 四、解析算法

1. **锚点**：固定引用取指定版本节点；非固定引用取目标条文的全部版本。
2. **建链**：沿接续边入边回溯得到链根（多根即天然分叉），再做多源 BFS
   记录根→各节点的最短前驱路径；只沿接续边（`revises`，及“废止并取代”的
   `repeals`）前向，遇到无接续的纯废止即终止。
3. **循环检测**：对锚点前向子图做迭代三色 DFS，发现灰→灰回边即报告
   `circular` 并给出环节点序列。
4. **效力判定**：取链上候选节点在目标日的窗口状态（终止时刻为自身
   `expires_at` 与纯废止时刻中较早者）——有效候选 ≥ 2 → `ambiguous`；
   恰 1 个 → `effective`；无有效候选时依次判定 `repealed`
   （含明文废止 / 失效 / 生效空档）、`not_yet_effective`。
5. **链路枚举**：从锚点做带 visited 的 DFS，枚举到每个叶子/废止点的
   简单路径（上限 `MAX_CHAIN_PATHS=64`、`MAX_CHAIN_DEPTH=200`），
   逐节点标注目标日状态，形成可追溯输出。

---

## 五、项目结构

```
app/
  main.py        FastAPI 路由与生命周期（SQLite WAL 连接；/rehearse 事务回滚）
  schemas.py     Pydantic 输入模型、枚举、UTC 时间规范化
  response.py    响应模型
  database.py    SQLite 建表与读写（幂等校验、悬空引用告警）
  resolver.py    修订链图、循环检测、分叉判定、链路枚举
scripts/
  seed_demo.py   演示语料写入脚本
tests/
  test_api.py       20 个端到端用例（各分类、时区、幂等、分叉、循环、纯废止时刻）
  test_rehearse.py  11 个预演用例（前后差异、不留痕、409/422、自带引用排除）
Dockerfile
docker-compose.yml
requirements.txt
```

## 六、测试

```bash
pip install -r requirements.txt
pytest -q
```

覆盖：UTC 归一化（带时区 / 无时区 / 仅日期）、重复写入 409、
链上条号变更解析、当日有效 / 已废止（明文废止终止点）/ 尚未生效、
固定版本引用与固定版本缺失、目标不存在、分叉歧义（含每条分叉链路）、
修订环、审查汇总计数、无接续纯废止的废止时刻（登记版本生效时间、
左闭右开、固定引用、接续条款解析、自身更早失效优先）；
以及登记前预演：跨文档/跨条号的结论/解析版本/链路差异、仅链路变化、
无变化返回空、纯废止与固定引用沿用 `/audit` 判定、拟登记版本自带引用
排除、按引用 ID 升序与过滤参数、预演成功或失败均不留痕且之后可正常
登记、重复版本 409 与非法关系 422 沿用 `/corpus` 错误响应。
