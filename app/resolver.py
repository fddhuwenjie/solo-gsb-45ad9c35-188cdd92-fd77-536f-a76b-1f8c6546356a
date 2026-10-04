"""修订链解析与引用审查核心逻辑。

修订链模型
----------
* 节点 = 一个具体条款版本 ``(doc_id, version, clause_id)``；
* ``revises`` 边 = 新版本接续旧版本（形成可继续延伸的修订链）；
* ``repeals`` 边 = 明文废止：带接续节点时表示“废止并由新法取代”（链延续，
  进入边标记为 repeals）；无接续节点时为链的终止点，并以**登记该废止的
  文档版本的生效时间**为废止时刻（UTC、左闭右开：到达该时刻即视为已废止）；
* 一个旧节点经 ``revises`` 分叉出两个新节点时，引用解析为**歧义**；
* 前向可达子图中出现环时判定为**循环**。

引用解析
--------
* 非固定引用：从被引条文的所有版本出发，沿 ``revises`` 边向前解析，
  链上所有节点都是该条文的历史/当前版本（条号、文档均可变化）；
* 固定版本引用（target_version）：只评价被固定的那个节点本身，
  但仍返回其完整可追溯链路。

审查状态：effective / repealed / not_yet_effective /
ambiguous / circular / not_found / broken_chain。
生效窗口采用左闭右开区间 ``[effective_at, expires_at)``。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Optional

from .schemas import AuditStatus, RelationType, STATUS_LABELS

Node = tuple[str, str, str]  # (doc_id, version, clause_id)

# 链遍历保护上限，防止异常数据导致指数膨胀
MAX_CHAIN_PATHS = 64
MAX_CHAIN_DEPTH = 200


# --------------------------------------------------------------------------- #
# 图
# --------------------------------------------------------------------------- #
class Graph:
    def __init__(self, clause_rows, edge_rows):
        # 条款版本节点数据
        self.nodes: dict[Node, dict[str, Any]] = {}
        for r in clause_rows:
            self.nodes[
                (r["doc_id"], r["version"], r["clause_id"])
            ] = {
                "title": r["title"],
                "text": r["text"],
                "effective_at": r["effective_at"],
                "expires_at": r["expires_at"],
            }

        # 接续边（可继续沿链前进）：pred -> [(kind, succ)]
        # revises 表示“修订后接续”；repeals 带 successor 表示“废止并由新法取代”
        self.continuations: dict[Node, list[tuple[str, Node]]] = {}
        # 纯废止（无接续）：pred -> [(note, recorded_by, repeal_at)]，链在此终止；
        # repeal_at 为登记该废止的文档版本的生效时间（ISO 字符串，可为 None）
        self.repeal_terminals: dict[
            Node, list[tuple[Optional[str], tuple, Optional[str]]]
        ] = {}
        # 基于接续边的入边（用于回溯链根）
        self.incoming: dict[Node, list[Node]] = {}
        self.edge_kind: dict[tuple[Node, Node], str] = {}
        self.edge_note: dict[tuple[Node, Node], Optional[str]] = {}

        for e in edge_rows:
            pred = (
                e["predecessor_doc"],
                e["predecessor_version"],
                e["predecessor_clause"],
            )
            succ: Optional[Node] = None
            if e["successor_doc"] is not None:
                succ = (
                    e["successor_doc"],
                    e["successor_version"],
                    e["successor_clause"],
                )
            recorded = (e["recorded_by_doc"], e["recorded_by_version"])
            if e["kind"] == "revises" or succ is not None:
                # 带接续节点的 repeals 同样使链延续，但边上保留“明文废止”语义
                self.continuations.setdefault(pred, []).append((e["kind"], succ))
                self.incoming.setdefault(succ, []).append(pred)
                self.edge_kind[(pred, succ)] = e["kind"]
                self.edge_note[(pred, succ)] = e["note"]
            else:
                # 无接续节点的 repeals —— 链的终止点；
                # 废止时刻取登记版本的生效时间（无则无法确定，不施加时间终止）
                self.repeal_terminals.setdefault(pred, []).append(
                    (e["note"], recorded, e["recorded_effective_at"])
                )

    # -- 工具 --------------------------------------------------------------- #
    @staticmethod
    def _parse(value: Optional[str]) -> Optional[datetime]:
        if value is None:
            return None
        return datetime.fromisoformat(value)

    def repeal_moment(self, key: Node) -> Optional[datetime]:
        """节点被无接续明文废止的生效时刻（多个登记取最早；无则 None）。"""
        moments = [
            m
            for m in (
                self._parse(repeal_at)
                for _, _, repeal_at in self.repeal_terminals.get(key, [])
            )
            if m is not None
        ]
        return min(moments) if moments else None

    def repealed_by_terminal(self, key: Node, target: datetime) -> bool:
        """目标时刻是否已达到该节点的明文废止时刻（左闭右开）。"""
        moment = self.repeal_moment(key)
        return moment is not None and target >= moment

    def window_state(self, key: Node, target: datetime) -> str:
        """单个版本节点在目标时刻的窗口状态。

        终止时刻取自身 ``expires_at`` 与无接续明文废止时刻（登记版本的
        生效时间）中较早者；左闭右开：到达该时刻即视为已废止。
        """
        data = self.nodes.get(key)
        if data is None:
            return AuditStatus.NOT_FOUND.value
        eff = self._parse(data["effective_at"])
        end = self._parse(data["expires_at"])
        repeal_at = self.repeal_moment(key)
        if repeal_at is not None and (end is None or repeal_at < end):
            end = repeal_at
        if eff is not None and target < eff:
            return AuditStatus.NOT_YET_EFFECTIVE.value
        if end is not None and target >= end:
            return AuditStatus.REPEALED.value
        return AuditStatus.EFFECTIVE.value

    def follow_successors(self, key: Node) -> list[Node]:
        return [succ for _, succ in self.continuations.get(key, [])]

    def all_matching(self, doc: str, clause: str) -> list[Node]:
        return sorted(
            k for k in self.nodes if k[0] == doc and k[2] == clause
        )


# --------------------------------------------------------------------------- #
# 组件（一条引用相关的修订链子图）
# --------------------------------------------------------------------------- #
class Component:
    def __init__(self, graph: Graph, anchors: list[Node], pinned: bool,
                 target_doc: str, target_clause: str):
        self.g = graph
        self.anchors = sorted(set(anchors))
        self.pinned = pinned
        self.target_doc = target_doc
        self.target_clause = target_clause

        self.roots, self.root_of, self.parent_from_root = self._roots()
        # 沿 revises 前向可达的全部节点（链身份沿边传播，允许条号变化）
        self.reachable = set(self.root_of.keys())
        self.cycle = self._detect_cycle()

    def _roots(self):
        """沿入边回溯链根，并做多源 BFS 记录最近根与前驱。"""
        g = self.g

        # 回溯阶段：收集根（无 revises 入边的节点）
        seeds = deque(self.anchors)
        seen: set[Node] = set()
        roots: set[Node] = set()
        while seeds:
            cur = seeds.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            preds = g.incoming.get(cur, [])
            if not preds:
                roots.add(cur)
            else:
                for p in preds:
                    if p not in seen:
                        seeds.append(p)

        if not roots:
            roots = set(self.anchors)  # 纯环结构

        # 多源 BFS：root_of / parent_from_root
        root_of: dict[Node, Node] = {}
        parent: dict[Node, Optional[Node]] = {}
        q = deque()
        for r in roots:
            root_of[r] = r
            parent[r] = None
            q.append(r)
        while q:
            cur = q.popleft()
            for succ in g.follow_successors(cur):
                if succ not in root_of:
                    root_of[succ] = root_of[cur]
                    parent[succ] = cur
                    q.append(succ)
        # 未被覆盖的锚点自成根（异常隔离）
        for a in self.anchors:
            if a not in root_of:
                root_of[a] = a
                parent[a] = None
                roots.add(a)
        return roots, root_of, parent

    def _detect_cycle(self) -> Optional[list[Node]]:
        """迭代 DFS（三色标记）检测锚点前向接续子图中的环。"""
        g = self.g
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[Node, int] = {}

        for start in self.anchors:
            if color.get(start) == BLACK:
                continue
            color[start] = GRAY
            stack: list[tuple[Node, int]] = [(start, 0)]
            while stack:
                u, idx = stack[-1]
                succs = g.follow_successors(u)
                if idx < len(succs):
                    stack[-1] = (u, idx + 1)
                    v = succs[idx]
                    if color.get(v, WHITE) == WHITE:
                        color[v] = GRAY
                        stack.append((v, 0))
                    elif color.get(v) == GRAY:
                        on_stack = [n for n, _ in stack]
                        try:
                            cut = on_stack.index(v)
                        except ValueError:
                            cut = 0
                        return on_stack[cut:] + [v]
                else:
                    color[u] = BLACK
                    stack.pop()
        return None

    # -- 候选与判定 ---------------------------------------------------------- #
    def candidate_rows(self) -> list[Node]:
        """参与效力评价的版本节点。

        * 固定引用：仅被固定的锚点自身；
        * 非固定引用：接续链上的全部真实节点（条号、文档均可变化）。
        """
        if self.pinned:
            return [a for a in self.anchors if a in self.g.nodes]
        return sorted(k for k in self.reachable if k in self.g.nodes)

    def classify(self, target: datetime) -> dict[str, Any]:
        g = self.g

        if self.cycle is not None:
            return {
                "status": AuditStatus.CIRCULAR,
                "reason": "修订链存在循环：" + self._cycle_text(self.cycle),
                "resolved": None,
                "effective_candidates": [],
            }

        rows = self.candidate_rows()
        effective_rows = [
            k for k in rows
            if g.window_state(k, target) == AuditStatus.EFFECTIVE.value
        ]

        if len(effective_rows) >= 2:
            branched = self._has_branch()
            kind = (
                "修订链分叉，存在多个接续分支"
                if branched
                else "多个版本的生效窗口在目标日期重叠"
            )
            return {
                "status": AuditStatus.AMBIGUOUS,
                "reason": f"{kind}，目标日期有 {len(effective_rows)} 个同时有效的版本",
                "resolved": None,
                "effective_candidates": effective_rows,
            }

        if len(effective_rows) == 1:
            node = effective_rows[0]
            return {
                "status": AuditStatus.EFFECTIVE,
                "reason": "目标日期存在有效版本",
                "resolved": node,
                "effective_candidates": [node],
            }

        # 无有效版本 —— 区分已废止 / 尚未生效
        expired_rows = [
            k for k in rows
            if g.window_state(k, target) == AuditStatus.REPEALED.value
        ]
        future_rows = [
            k for k in rows
            if g.window_state(k, target) == AuditStatus.NOT_YET_EFFECTIVE.value
        ]

        if expired_rows:
            # 链末端是否存在已达到废止时刻的无接续明文废止
            terminal_nodes = {
                k for k in expired_rows if g.repealed_by_terminal(k, target)
            }
            if terminal_nodes:
                reason = "最新版本已被明文废止"
            else:
                # 经 repeals 边被新法取代，或仅因失效窗口到期
                replaced = any(
                    kind == "repeals"
                    for k in expired_rows
                    for kind, _ in g.continuations.get(k, [])
                )
                reason = (
                    self._gap_reason(rows, target)
                    or ("已被明文废止并由新法取代" if replaced else "最新版本已过失效时间")
                )
            return {
                "status": AuditStatus.REPEALED,
                "reason": reason,
                "resolved": None,
                "effective_candidates": [],
            }

        if future_rows:
            nxt = min(
                future_rows,
                key=lambda k: g.nodes[k]["effective_at"] or "",
            )
            return {
                "status": AuditStatus.NOT_YET_EFFECTIVE,
                "reason": (
                    "最早版本生效时间为 "
                    f"{g.nodes[nxt]['effective_at']}，目标日期尚未生效"
                ),
                "resolved": None,
                "effective_candidates": [],
            }

        return {
            "status": AuditStatus.BROKEN_CHAIN,
            "reason": "引用锚点所在修订链上不存在目标条文版本（链断裂或锚点不存在）",
            "resolved": None,
            "effective_candidates": [],
        }

    def _has_branch(self) -> bool:
        return any(
            len(self.g.continuations.get(k, [])) >= 2 for k in self.reachable
        )

    def _gap_reason(self, rows: list[Node], target: datetime) -> Optional[str]:
        """检测生效空档：旧版失效早于与其接续的新版生效。"""
        g = self.g
        for prev in rows:
            exp = g._parse(g.nodes[prev]["expires_at"])
            if not exp:
                continue
            for _, succ in g.continuations.get(prev, []):
                if succ not in g.nodes:
                    continue
                eff = g._parse(g.nodes[succ]["effective_at"])
                if eff and exp < eff:
                    return (
                        "修订链存在生效空档：旧版失效于 "
                        f"{exp.isoformat()}，新版生效于 {eff.isoformat()}"
                    )
        return None

    @staticmethod
    def _cycle_text(cycle: list[Node]) -> str:
        return " -> ".join(
            f"{doc}@{ver}#{clause}" for doc, ver, clause in cycle
        )

    # -- 可追溯链路 ---------------------------------------------------------- #
    def build_chains(self, target: datetime) -> list[dict[str, Any]]:
        """为每个锚点生成从链根到各终止点的完整路径（含每个分叉）。"""
        chains: list[dict[str, Any]] = []
        for anchor in self.anchors:
            prefix = self._prefix_to(anchor)
            suffixes = self._enumerate_suffixes(anchor)
            for idx, (suffix, terminal) in enumerate(suffixes):
                steps = [self._step(prefix[0], None, target)]
                for prev, cur in zip(prefix, prefix[1:]):
                    steps.append(self._step(cur, prev, target))
                # suffix 首节点即锚点（与 prefix 末端重合），从第二条边开始追加
                for prev, cur in zip(suffix, suffix[1:]):
                    steps.append(self._step(cur, prev, target))
                if terminal is not None:
                    steps.append(self._terminal_step(terminal, target))
                chains.append(
                    {
                        "anchor": _node_ref(anchor),
                        "branch_index": idx,
                        "steps": steps,
                    }
                )
        return chains

    def _prefix_to(self, anchor: Node) -> list[Node]:
        """由 BFS 父链反向得到 root -> anchor 的最短路径。"""
        if anchor in self.roots and self.parent_from_root.get(anchor) is None:
            return [anchor]
        path = [anchor]
        cur = self.parent_from_root.get(anchor)
        guard = 0
        while cur is not None and guard <= MAX_CHAIN_DEPTH:
            path.append(cur)
            cur = self.parent_from_root.get(cur)
            guard += 1
        path.reverse()
        return path

    def _enumerate_suffixes(
        self, start: Node
    ) -> list[tuple[list[Node], Optional[tuple]]]:
        """从锚点枚举全部简单前向路径。

        每条路径为 (节点序列, 纯废止终止信息)；沿接续边（含
        “废止并取代”）前进。无接续的 repeals 记为终止分支；若同一节点
        另有接续边，链仍沿接续边延续（与效力判定的候选集一致）。
        """
        g = self.g
        results: list[tuple[list[Node], Optional[tuple]]] = []

        def dfs(path: list[Node], visited: set[Node]):
            cur = path[-1]
            terminals = g.repeal_terminals.get(cur, [])
            # 纯废止：登记为终止分支
            for term in terminals:
                if len(results) < MAX_CHAIN_PATHS:
                    results.append((list(path), term))
            conts = g.continuations.get(cur, [])
            if not conts:
                if not terminals:
                    results.append((list(path), None))
                return
            for _, succ in conts:
                if len(results) >= MAX_CHAIN_PATHS:
                    return
                if succ in visited or len(path) >= MAX_CHAIN_DEPTH:
                    results.append((list(path), None))  # 环/深度保护截断
                    continue
                visited.add(succ)
                dfs(path + [succ], visited)
                visited.discard(succ)

        dfs([start], {start})
        if not results:
            results = [([start], None)]
        return results

    def _step(self, node: Node, prev: Optional[Node], target: datetime) -> dict:
        g = self.g
        data = g.nodes.get(node, {})
        return {
            "node": _node_ref(node),
            "title": data.get("title"),
            "effective_at": data.get("effective_at"),
            "expires_at": data.get("expires_at"),
            "state_at_target": g.window_state(node, target),
            "edge_in": g.edge_kind.get((prev, node)) if prev else None,
            "terminal": False,
            "note": g.edge_note.get((prev, node)) if prev else None,
            "recorded_by": None,
            "exists_in_corpus": node in g.nodes,
        }

    def _terminal_step(self, terminal: tuple, target: datetime) -> dict:
        """无接续 repeals 的终止步（废止事件，不含条文节点）。

        ``effective_at`` 为废止生效时刻（登记版本的生效时间）；
        目标时刻未达到时标记为尚未生效，达到后标记为已废止，
        与审查结论保持一致。
        """
        note, recorded, repeal_at = terminal
        repeal_dt = self.g._parse(repeal_at)
        state = (
            AuditStatus.REPEALED.value
            if repeal_dt is None or target >= repeal_dt
            else AuditStatus.NOT_YET_EFFECTIVE.value
        )
        return {
            "node": None,
            "title": None,
            "effective_at": repeal_at,
            "expires_at": None,
            "state_at_target": state,
            "edge_in": RelationType.REPEALS.value,
            "terminal": True,
            "note": note,
            "recorded_by": {"doc_id": recorded[0], "version": recorded[1]},
            "exists_in_corpus": False,
        }


# --------------------------------------------------------------------------- #
# 审查入口
# --------------------------------------------------------------------------- #
def audit_citation(graph: Graph, citation, target: datetime) -> dict[str, Any]:
    """解析单条引用。citation 为 sqlite Row。"""
    target_doc = citation["target_doc"]
    target_clause = citation["target_clause"]
    pinned_version = citation["target_version"]

    any_version = graph.all_matching(target_doc, target_clause)

    if pinned_version is not None:
        anchor: Node = (target_doc, pinned_version, target_clause)
        if anchor not in graph.nodes:
            status = (
                AuditStatus.NOT_FOUND
                if not any_version
                else AuditStatus.BROKEN_CHAIN
            )
            reason = (
                "被引文档条文在语料库中不存在任何版本"
                if not any_version
                else f"被引固定版本 {pinned_version} 不存在或与修订链断裂"
            )
            return _result(
                citation, target, pinned=True, status=status,
                reason=reason, anchors=[anchor], chains=[], cycle=None,
                resolved=None, effective_candidates=[],
            )
        anchors = [anchor]
        pinned = True
    else:
        anchors = any_version
        pinned = False
        if not anchors:
            return _result(
                citation, target, pinned=False,
                status=AuditStatus.NOT_FOUND,
                reason="被引文档条文在语料库中不存在任何版本",
                anchors=[], chains=[], cycle=None,
                resolved=None, effective_candidates=[],
            )

    comp = Component(graph, anchors, pinned, target_doc, target_clause)
    verdict = comp.classify(target)

    if (
        verdict["status"] == AuditStatus.BROKEN_CHAIN
        and not pinned
        and any_version
    ):
        verdict["reason"] = "被引条文的各版本与修订关系无法连通（链断裂）"

    chains = comp.build_chains(target)
    return _result(
        citation, target,
        pinned=pinned,
        status=verdict["status"],
        reason=verdict["reason"],
        anchors=anchors,
        chains=chains,
        cycle=comp.cycle,
        resolved=verdict["resolved"],
        effective_candidates=verdict["effective_candidates"],
    )


def _node_ref(node: Optional[Node]) -> Optional[dict[str, str]]:
    if node is None:
        return None
    return {"doc_id": node[0], "version": node[1], "clause_id": node[2]}


def _result(
    citation, target, *, pinned: bool, status: AuditStatus, reason: str,
    anchors: list[Node], chains: list, cycle: Optional[list[Node]],
    resolved: Optional[Node], effective_candidates: list[Node],
) -> dict[str, Any]:
    return {
        "citation_id": citation["id"],
        "source": {
            "doc_id": citation["source_doc"],
            "version": citation["source_version"],
            "clause_id": citation["source_clause"],
        },
        "target": {
            "doc_id": citation["target_doc"],
            "clause_id": citation["target_clause"],
            "pinned_version": citation["target_version"],
        },
        "pinned": pinned,
        "context": citation["context"],
        "target_at": target.isoformat(),
        "status": status.value,
        "status_label": STATUS_LABELS[status],
        "reason": reason,
        "resolved": _node_ref(resolved),
        "effective_candidates": [_node_ref(k) for k in effective_candidates],
        "cycle": [_node_ref(n) for n in cycle] if cycle else None,
        "anchors": [_node_ref(a) for a in anchors],
        "chains": chains,
    }


def run_audit(graph: Graph, citation_rows, target: datetime) -> dict[str, Any]:
    results = [audit_citation(graph, c, target) for c in citation_rows]
    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {
        "target_at": target.isoformat(),
        "total": len(results),
        "summary": summary,
        "results": results,
    }
