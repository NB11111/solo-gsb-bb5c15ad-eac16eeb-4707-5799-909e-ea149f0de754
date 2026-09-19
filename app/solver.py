"""全局流量匹配求解器。

将评审分配问题整体规约为二部 b-匹配，用最大流（Edmonds-Karp）一次求解，
而不是逐稿贪心：

    源点 --(所需评审数)--> 稿件 --(1, 仅当可评审)--> 评审人 --(容量)--> 汇点

- 稿件 -> 评审人 的边仅当：主题能力匹配、无利益冲突、非自稿。
- 最大流 == 总需求 时存在满足全部约束的分配；否则判定不可行并给出缺口诊断。
- 所有节点按 ID 排序后建图，BFS 按固定邻接顺序遍历，算法完全确定：
  相同输入（甚至不同的列表顺序）必然产生完全相同的分配结果。

分赛道容量（稿件携带 track、评审人携带 track_capacities 时）：

- 稿件带赛道 t 且评审人在 track_capacities 中列出 t 时，其流量必须经过
  专属「(评审人, t) 赛道节点」：稿件 -> 赛道节点(容量 1) -> 评审人
  (容量=赛道限额) -> 汇点(容量=总容量)，两级容量串联，一次建图同时满足，
  绝不分赛道独立求解后拼接；
- 评审人未列出的赛道（以及不带赛道的稿件）直接连评审人，只受总容量约束。

增量重排模式（请求携带 previous_assignments / locked_assignments 时）：

- 锁定关系先逐一校验稿件需求、评审资格、总容量与赛道限额，任一冲突即整体
  不可行，返回结构化冲突原因，不输出任何部分分配；
- 合法锁定关系固化为必留边（扣减稿件剩余需求与评审剩余总容量/赛道限额），
  其余名额用最小费用最大流求解：复用 previous_assignments 的边费用为 0、
  新增边费用为 1。历史关系仅在总容量与赛道限额两级均允许时才可能复用：
  流量固定时费用最小 <=> 复用上一轮的边最多 <=> 新增+移除的关系总数最少；
- 增广路按 (费用, 节点下标) 字典序选取，节点下标来自 ID 排序：
  同成本解由 ID 顺序确定性地破除，输出完全确定。
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ManuscriptSpec:
    id: str
    authors: tuple[str, ...]
    topics: tuple[str, ...]
    required_reviewers: int
    track: str | None = None


@dataclass(frozen=True)
class ReviewerSpec:
    id: str
    topics: tuple[str, ...]
    capacity: int
    conflicted_authors: tuple[str, ...]
    conflicted_manuscripts: tuple[str, ...]
    # (赛道, 限额) 排序元组；空元组表示不设任何赛道限额
    track_capacities: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class UnsatisfiedManuscript:
    manuscript_id: str
    required: int
    assigned: int
    missing: int
    eligible_reviewers: int
    eligible_remaining_capacity: int
    candidate_gap: int
    capacity_gap: int


# 锁定关系冲突类型：
#   unknown_manuscript / unknown_reviewer  —— 引用了不存在的稿件 / 评审人
#   self_conflict / author_conflict / manuscript_conflict / topic_mismatch —— 评审资格
#   reviewer_capacity_exceeded             —— 锁定数超出评审总容量
#   reviewer_track_capacity_exceeded       —— 锁定数超出评审人某赛道限额
#   manuscript_requirement_exceeded        —— 锁定数超出稿件所需评审数
@dataclass(frozen=True)
class LockedConflict:
    type: str
    manuscript_id: str | None
    reviewer_id: str | None
    locked: int | None
    limit: int | None
    detail: str
    # 仅分赛道容量冲突携带：超出限额的赛道
    track: str | None = None


@dataclass(frozen=True)
class SolveResult:
    feasible: bool
    # manuscript_id -> 已排序的 reviewer id 元组；不可行时为空（不输出部分结果）
    assignments: dict[str, tuple[str, ...]]
    total_required: int
    total_assigned: int
    unsatisfied: tuple[UnsatisfiedManuscript, ...]
    shared_pool: dict | None
    # 锁定关系冲突（增量重排模式）；无冲突或普通模式时为空
    locked_conflicts: tuple[LockedConflict, ...] = ()


class _MaxFlow:
    """Edmonds-Karp 最大流。邻接表按插入顺序遍历，建图顺序确定则结果确定。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self.cap = [[0] * n for _ in range(n)]
        self.adj: list[list[int]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, c: int) -> None:
        if c <= 0:
            return
        self.adj[u].append(v)
        self.adj[v].append(u)
        self.cap[u][v] += c

    def max_flow(self, s: int, t: int) -> int:
        flow = 0
        while True:
            parent = [-1] * self.n
            parent[s] = s
            q = deque([s])
            while q and parent[t] == -1:
                u = q.popleft()
                for v in self.adj[u]:
                    if parent[v] == -1 and self.cap[u][v] > 0:
                        parent[v] = u
                        q.append(v)
            if parent[t] == -1:
                return flow
            bottleneck = None
            v = t
            while v != s:
                u = parent[v]
                c = self.cap[u][v]
                bottleneck = c if bottleneck is None else min(bottleneck, c)
                v = u
            v = t
            while v != s:
                u = parent[v]
                self.cap[u][v] -= bottleneck
                self.cap[v][u] += bottleneck
                v = u
            flow += bottleneck


class _MinCostMaxFlow:
    """SSP 最小费用最大流（初始费用非负，Dijkstra + 势函数）。

    堆元素为 (费用, 顶点下标)，松弛严格小于才更新：同费用路径取下标最小者，
    建图顺序确定则每次增广路确定，整体结果完全确定。
    """

    def __init__(self, n: int) -> None:
        self.n = n
        # 边: [to, 残余容量, 单位费用, 反向边在 g[to] 中的下标]
        self.g: list[list[list[int]]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> int:
        """返回正向边在 g[u] 中的下标（容量为 0 的边同样建边，便于诊断读取）。"""
        fwd = [v, cap, cost, len(self.g[v])]
        rev = [u, 0, -cost, len(self.g[u])]
        self.g[u].append(fwd)
        self.g[v].append(rev)
        return len(self.g[u]) - 1

    def residual(self, u: int, ei: int) -> int:
        """正向边 (u, ei) 的残余容量。"""
        return self.g[u][ei][1]

    def flow_on(self, u: int, ei: int) -> int:
        """正向边 (u, ei) 上的流量（即反向边的残余容量）。"""
        e = self.g[u][ei]
        return self.g[e[0]][e[3]][1]

    def min_cost_max_flow(self, s: int, t: int) -> tuple[int, int]:
        n = self.n
        g = self.g
        potential = [0] * n
        flow = 0
        cost = 0
        INF = 1 << 60
        while True:
            dist = [INF] * n
            prev_v = [-1] * n
            prev_e = [-1] * n
            dist[s] = 0
            pq = [(0, s)]
            while pq:
                d, u = heapq.heappop(pq)
                if d > dist[u]:
                    continue
                for i, e in enumerate(g[u]):
                    if e[1] <= 0:
                        continue
                    v = e[0]
                    nd = d + e[2] + potential[u] - potential[v]
                    if nd < dist[v]:
                        dist[v] = nd
                        prev_v[v] = u
                        prev_e[v] = i
                        heapq.heappush(pq, (nd, v))
            if dist[t] == INF:
                return flow, cost
            for v in range(n):
                if dist[v] < INF:
                    potential[v] += dist[v]
            d = INF
            v = t
            while v != s:
                e = g[prev_v[v]][prev_e[v]]
                if e[1] < d:
                    d = e[1]
                v = prev_v[v]
            v = t
            while v != s:
                e = g[prev_v[v]][prev_e[v]]
                e[1] -= d
                g[v][e[3]][1] += d
                v = prev_v[v]
            flow += d
            cost += d * potential[t]


def _eligible_reviewers(m: ManuscriptSpec, reviewers: list[ReviewerSpec]) -> list[int]:
    """返回可评审该稿件的评审人下标列表（按评审人排序后的顺序）。"""
    m_topics = set(m.topics)
    authors = set(m.authors)
    out: list[int] = []
    for j, r in enumerate(reviewers):
        if r.id in authors:
            continue  # 自稿冲突：评审人是稿件作者
        if authors.intersection(r.conflicted_authors):
            continue  # 利益冲突：与稿件作者冲突
        if m.id in r.conflicted_manuscripts:
            continue  # 利益冲突：直接声明回避该稿件
        if m_topics and not m_topics.intersection(r.topics):
            continue  # 主题能力不匹配
        out.append(j)
    return out


_REASON_DETAIL = {
    "self_conflict": "评审人是稿件作者（自稿冲突）",
    "author_conflict": "评审人与稿件作者存在利益冲突",
    "manuscript_conflict": "评审人已声明回避该稿件",
    "topic_mismatch": "评审人主题能力与稿件主题不匹配",
}


def _pair_ineligibility_reasons(m: ManuscriptSpec, r: ReviewerSpec) -> tuple[str, ...]:
    """返回 (m, r) 不满足评审资格的全部原因；空元组表示合格。

    判定条件必须与 _eligible_reviewers 保持一致。
    """
    authors = set(m.authors)
    reasons: list[str] = []
    if r.id in authors:
        reasons.append("self_conflict")
    if authors.intersection(r.conflicted_authors):
        reasons.append("author_conflict")
    if m.id in r.conflicted_manuscripts:
        reasons.append("manuscript_conflict")
    if m.topics and not set(m.topics).intersection(r.topics):
        reasons.append("topic_mismatch")
    return tuple(reasons)


@dataclass(frozen=True)
class _GraphLayout:
    """流网络的节点布局。

    节点编号：源点 0；稿件 1..m_count；评审人随后；再后为「(评审人, 赛道)」
    赛道节点（仅覆盖评审人 track_capacities 中列出、且被某稿件使用的赛道，
    按 (赛道, 评审下标) 排序分配）；汇点固定为最后一个编号。
    无任何赛道限额时布局退化为历史版本（汇点编号不变）。
    """

    m_count: int
    r_count: int
    src: int
    snk: int
    n_nodes: int
    # (评审下标, 赛道) -> 赛道节点下标
    rt_nodes: dict[tuple[int, str], int]

    def m_node(self, i: int) -> int:
        return 1 + i

    def r_node(self, j: int) -> int:
        return 1 + self.m_count + j

    def rt_node(self, j: int, track: str) -> int:
        return self.rt_nodes[(j, track)]


def _build_layout(ms: list[ManuscriptSpec], rs: list[ReviewerSpec]) -> _GraphLayout:
    """按确定顺序分配赛道节点；不使用赛道时与历史布局逐字节一致。"""
    m_count, r_count = len(ms), len(rs)
    used_tracks = sorted({m.track for m in ms if m.track is not None})
    rt_nodes: dict[tuple[int, str], int] = {}
    next_index = 1 + m_count + r_count
    for t in used_tracks:
        for j, r in enumerate(rs):
            if dict(r.track_capacities).get(t) is not None:
                rt_nodes[(j, t)] = next_index
                next_index += 1
    snk = next_index
    return _GraphLayout(
        m_count=m_count,
        r_count=r_count,
        src=0,
        snk=snk,
        n_nodes=snk + 1,
        rt_nodes=rt_nodes,
    )


def solve_assignment(
    manuscripts: list[ManuscriptSpec],
    reviewers: list[ReviewerSpec],
    previous: frozenset[tuple[str, str]] | None = None,
    locked: frozenset[tuple[str, str]] | None = None,
) -> SolveResult:
    """求解分配。

    previous / locked 为 (manuscript_id, reviewer_id) 对集合；两者都缺省时
    走普通模式（行为与历史版本完全一致），任一提供即进入增量重排模式。
    """
    # 按 ID 排序，保证建图顺序与输入列表顺序无关 -> 输出稳定
    ms = sorted(manuscripts, key=lambda m: m.id)
    rs = sorted(reviewers, key=lambda r: r.id)
    if previous is None and locked is None:
        return _solve_normal(ms, rs)
    return _solve_incremental(
        ms,
        rs,
        previous if previous is not None else frozenset(),
        locked if locked is not None else frozenset(),
    )


def _solve_normal(ms: list[ManuscriptSpec], rs: list[ReviewerSpec]) -> SolveResult:
    layout = _build_layout(ms, rs)
    SRC, SNK = layout.src, layout.snk

    track_cap = [dict(r.track_capacities) for r in rs]

    eligible = [_eligible_reviewers(m, rs) for m in ms]

    mf = _MaxFlow(layout.n_nodes)
    for i, m in enumerate(ms):
        mf.add_edge(SRC, layout.m_node(i), m.required_reviewers)
    # 先建全部稿件侧出边（含赛道节点 -> 评审人），再建评审人 -> 汇点，
    # 使评审人节点的邻接顺序与无赛道时完全一致，输出确定。
    for i, m in enumerate(ms):
        for j in eligible[i]:
            if m.track is not None and m.track in track_cap[j]:
                # 带赛道且评审人列出该赛道限额：经赛道节点串联两级容量
                mf.add_edge(layout.m_node(i), layout.rt_node(j, m.track), 1)
            else:
                # 评审人未列出该赛道（或稿件无赛道）：只受总容量约束
                mf.add_edge(layout.m_node(i), layout.r_node(j), 1)
    # 赛道节点 -> 评审人：每个赛道节点恰好一条，容量为该赛道限额
    for (j, t), rt in sorted(layout.rt_nodes.items(), key=lambda kv: kv[1]):
        mf.add_edge(rt, layout.r_node(j), track_cap[j][t])
    for j, r in enumerate(rs):
        mf.add_edge(layout.r_node(j), SNK, r.capacity)

    total_required = sum(m.required_reviewers for m in ms)
    total_assigned = mf.max_flow(SRC, SNK)

    # 提取分配：直接边与赛道节点路径均以「稿件出边的反向残余容量」计量，
    # 该残余只出现在原始 m->r / m->rt 边上，且原始容量为 1。
    def _has_flow(i: int, j: int) -> bool:
        m = ms[i]
        if m.track is not None and m.track in track_cap[j]:
            return mf.cap[layout.rt_node(j, m.track)][layout.m_node(i)] > 0
        return mf.cap[layout.r_node(j)][layout.m_node(i)] > 0

    flow_assignment: dict[str, tuple[str, ...]] = {}
    for i, m in enumerate(ms):
        picked = tuple(
            sorted(rs[j].id for j in eligible[i] if _has_flow(i, j))
        )
        flow_assignment[m.id] = picked

    if total_assigned == total_required:
        return SolveResult(
            feasible=True,
            assignments=flow_assignment,
            total_required=total_required,
            total_assigned=total_assigned,
            unsatisfied=(),
            shared_pool=None,
        )

    # ---- 不可行：诊断缺口，不输出任何部分分配 ----
    def _usable_normal(i: int, j: int) -> int:
        """该 (稿件, 评审人) 对当前还能占用的容量：受赛道限额与总容量共同约束。"""
        m = ms[i]
        sink_residual = mf.cap[layout.r_node(j)][SNK]
        if m.track is not None and m.track in track_cap[j]:
            rt = layout.rt_node(j, m.track)
            return min(sink_residual, mf.cap[rt][layout.r_node(j)])
        return sink_residual

    unsatisfied: list[UnsatisfiedManuscript] = []
    unsat_idx: list[int] = []
    for i, m in enumerate(ms):
        assigned = len(flow_assignment[m.id])
        if assigned >= m.required_reviewers:
            continue
        missing = m.required_reviewers - assigned
        elig = eligible[i]
        remaining = sum(_usable_normal(i, j) for j in elig)
        unsatisfied.append(
            UnsatisfiedManuscript(
                manuscript_id=m.id,
                required=m.required_reviewers,
                assigned=assigned,
                missing=missing,
                eligible_reviewers=len(elig),
                eligible_remaining_capacity=remaining,
                # 候选缺口：合格候选人数本身不足（容量再大也不够）
                candidate_gap=max(0, m.required_reviewers - len(elig)),
                # 容量缺口：候选存在但剩余容量（赛道限额 / 总容量）不足
                capacity_gap=max(0, missing - remaining),
            )
        )
        unsat_idx.append(i)

    # 所有未满足稿件共享的候选池：解释“缺口到底缺在哪”。
    # 池内剩余容量对每个池内评审人取「各未满足稿件视角（赛道限额与总容量瓶颈）
    # 可用量」之和：无赛道时各稿件视角相同，即为该评审人的总剩余容量（历史语义）。
    pool = sorted({j for i in unsat_idx for j in eligible[i]})
    elig_sets = [set(e) for e in eligible]
    shared_pool = {
        "manuscript_ids": [ms[i].id for i in unsat_idx],
        "reviewer_ids": [rs[j].id for j in pool],
        "missing_total": sum(u.missing for u in unsatisfied),
        "pool_remaining_capacity": sum(
            sum(_usable_normal(i, j) for i in unsat_idx if j in elig_sets[i])
            for j in pool
        ),
        "pool_total_capacity": sum(rs[j].capacity for j in pool),
    }

    return SolveResult(
        feasible=False,
        assignments={},
        total_required=total_required,
        total_assigned=total_assigned,
        unsatisfied=tuple(unsatisfied),
        shared_pool=shared_pool,
    )


def _check_locked(
    ms: list[ManuscriptSpec],
    rs: list[ReviewerSpec],
    locked: frozenset[tuple[str, str]],
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, int],
    dict[tuple[str, str], int],
    tuple[LockedConflict, ...],
]:
    """校验锁定关系：评审资格（逐对）、评审总容量、分赛道限额、稿件需求。

    返回 (每个稿件合法锁定的评审人元组, 评审人 -> 合法锁定数,
    (评审人, 赛道) -> 合法锁定数, 冲突列表)；冲突非空即整体不可行。
    赛道计数只统计稿件带该赛道且评审人 track_capacities 列出该赛道的锁定。
    """
    m_by_id = {m.id: m for m in ms}
    r_by_id = {r.id: r for r in rs}

    conflicts: list[LockedConflict] = []
    locked_by_m: dict[str, list[str]] = {}
    locked_by_r: dict[str, list[str]] = {}
    for mid, rid in sorted(locked):
        m = m_by_id.get(mid)
        if m is None:
            conflicts.append(LockedConflict(
                type="unknown_manuscript", manuscript_id=mid, reviewer_id=rid,
                locked=None, limit=None, track=None,
                detail=f"锁定关系引用了不存在的稿件 {mid}",
            ))
            continue
        r = r_by_id.get(rid)
        if r is None:
            conflicts.append(LockedConflict(
                type="unknown_reviewer", manuscript_id=mid, reviewer_id=rid,
                locked=None, limit=None, track=None,
                detail=f"锁定关系引用了不存在的评审人 {rid}",
            ))
            continue
        reasons = _pair_ineligibility_reasons(m, r)
        for reason in reasons:
            conflicts.append(LockedConflict(
                type=reason, manuscript_id=mid, reviewer_id=rid,
                locked=None, limit=None, track=None,
                detail=f"锁定关系 {mid} -> {rid} 不满足评审资格：{_REASON_DETAIL[reason]}",
            ))
        if not reasons:
            locked_by_m.setdefault(mid, []).append(rid)
            locked_by_r.setdefault(rid, []).append(mid)

    # 合法锁定对在总容量 / 赛道限额 / 稿件需求三个维度的占用
    locked_count_r: dict[str, int] = {}
    locked_count_rt: dict[tuple[str, str], int] = {}
    for mid in sorted(locked_by_m):
        m = m_by_id[mid]
        for rid in locked_by_m[mid]:
            locked_count_r[rid] = locked_count_r.get(rid, 0) + 1
            if m.track is not None:
                r = r_by_id[rid]
                if dict(r.track_capacities).get(m.track) is not None:
                    key = (rid, m.track)
                    locked_count_rt[key] = locked_count_rt.get(key, 0) + 1

    # 总容量与赛道限额校验
    for rid in sorted(locked_by_r):
        r = r_by_id[rid]
        n_locked = len(locked_by_r[rid])
        if n_locked > r.capacity:
            conflicts.append(LockedConflict(
                type="reviewer_capacity_exceeded", manuscript_id=None, reviewer_id=rid,
                locked=n_locked, limit=r.capacity, track=None,
                detail=f"评审人 {rid} 被锁定 {n_locked} 篇稿件，超出其容量 {r.capacity}",
            ))
        cap_map = dict(r.track_capacities)
        for track in sorted(t for rr, t in locked_count_rt if rr == rid):
            n_track = locked_count_rt[(rid, track)]
            limit = cap_map[track]
            if n_track > limit:
                conflicts.append(LockedConflict(
                    type="reviewer_track_capacity_exceeded",
                    manuscript_id=None, reviewer_id=rid,
                    locked=n_track, limit=limit, track=track,
                    detail=f"评审人 {rid} 在赛道 {track} 被锁定 {n_track} 篇稿件，"
                           f"超出其该赛道限额 {limit}",
                ))
    for mid in sorted(locked_by_m):
        m = m_by_id[mid]
        n_locked = len(locked_by_m[mid])
        if n_locked > m.required_reviewers:
            conflicts.append(LockedConflict(
                type="manuscript_requirement_exceeded", manuscript_id=mid, reviewer_id=None,
                locked=n_locked, limit=m.required_reviewers, track=None,
                detail=f"稿件 {mid} 锁定 {n_locked} 名评审，超出其所需评审数 "
                       f"{m.required_reviewers}",
            ))

    conflicts.sort(
        key=lambda c: (
            c.type,
            c.manuscript_id or "",
            c.reviewer_id or "",
            c.track or "",
        )
    )
    locked_map = {mid: tuple(sorted(rids)) for mid, rids in locked_by_m.items()}
    return locked_map, locked_count_r, locked_count_rt, tuple(conflicts)


def _solve_incremental(
    ms: list[ManuscriptSpec],
    rs: list[ReviewerSpec],
    previous: frozenset[tuple[str, str]],
    locked: frozenset[tuple[str, str]],
) -> SolveResult:
    """增量重排：锁定关系必须全部成立；其余名额在全局可行前提下最少改动。"""
    total_required = sum(m.required_reviewers for m in ms)

    # ---- 1. 校验锁定关系：任一冲突即整体不可行，不输出部分分配 ----
    locked_map, locked_count_r, locked_count_rt, conflicts = _check_locked(ms, rs, locked)
    if conflicts:
        return SolveResult(
            feasible=False,
            assignments={},
            total_required=total_required,
            total_assigned=0,
            unsatisfied=(),
            shared_pool=None,
            locked_conflicts=conflicts,
        )

    locked_pairs = {(mid, rid) for mid, rids in locked_map.items() for rid in rids}

    layout = _build_layout(ms, rs)
    SRC, SNK = layout.src, layout.snk

    track_cap = [dict(r.track_capacities) for r in rs]

    eligible = [_eligible_reviewers(m, rs) for m in ms]

    # ---- 2. 锁定对固化后，对剩余需求做最小费用最大流 ----
    # 复用 previous 的边费用 0、新增边费用 1：流量固定时费用最小
    # <=> 复用上一轮的分配最多 <=> 新增 + 移除的关系总数最少。
    # 带赛道的稿件经赛道节点串联两级容量：历史关系只有在总容量与赛道限额
    # 两级都允许（对应边可增广）时才能被复用，无需分赛道拼接。
    mf = _MinCostMaxFlow(layout.n_nodes)
    for i, m in enumerate(ms):
        remaining_req = m.required_reviewers - len(locked_map.get(m.id, ()))
        mf.add_edge(SRC, layout.m_node(i), remaining_req, 0)
    mr_edges: list[tuple[int, int, int]] = []  # (稿件下标, 边下标, 评审下标)
    for i, m in enumerate(ms):
        for j in eligible[i]:
            if (m.id, rs[j].id) in locked_pairs:
                continue  # 锁定对已固化，不再作为候选边
            if m.track is not None and m.track in track_cap[j]:
                rt = layout.rt_node(j, m.track)
                # 费用记在 m->rt 边上：复用 previous 为 0，新增为 1
                ei = mf.add_edge(layout.m_node(i), rt, 1,
                                 0 if (m.id, rs[j].id) in previous else 1)
            else:
                ei = mf.add_edge(
                    layout.m_node(i), layout.r_node(j), 1,
                    0 if (m.id, rs[j].id) in previous else 1,
                )
            mr_edges.append((i, ei, j))
    # 赛道节点 -> 评审人：每个赛道节点恰好一条，容量为赛道限额扣除该赛道锁定占用
    rt_edge_idx: dict[tuple[int, str], int] = {}
    for (j, t), rt in sorted(layout.rt_nodes.items(), key=lambda kv: kv[1]):
        track_remaining = track_cap[j][t] - locked_count_rt.get((rs[j].id, t), 0)
        rt_edge_idx[(j, t)] = mf.add_edge(rt, layout.r_node(j), track_remaining, 0)
    r_edge_idx: list[int] = []
    for j, r in enumerate(rs):
        # 评审人 -> 汇点：总容量扣减全部锁定占用
        remaining_cap = r.capacity - locked_count_r.get(r.id, 0)
        r_edge_idx.append(mf.add_edge(layout.r_node(j), SNK, remaining_cap, 0))

    total_remaining = sum(
        m.required_reviewers - len(locked_map.get(m.id, ())) for m in ms
    )
    flow, _cost = mf.min_cost_max_flow(SRC, SNK)

    # 提取分配：锁定对 + 流量对（m->r / m->rt 边的流量均为 1）
    flow_by_m: dict[str, list[str]] = {m.id: [] for m in ms}
    for i, ei, j in mr_edges:
        if mf.flow_on(layout.m_node(i), ei) > 0:
            flow_by_m[ms[i].id].append(rs[j].id)
    assignment: dict[str, tuple[str, ...]] = {}
    for m in ms:
        picked = tuple(sorted(list(locked_map.get(m.id, ())) + flow_by_m[m.id]))
        assignment[m.id] = picked
    total_assigned = sum(len(v) for v in assignment.values())

    if flow == total_remaining:
        return SolveResult(
            feasible=True,
            assignments=assignment,
            total_required=total_required,
            total_assigned=total_assigned,
            unsatisfied=(),
            shared_pool=None,
        )

    # ---- 3. 不可行：诊断缺口（残余容量已扣除锁定占用），不输出部分分配 ----
    def _usable_incremental(i: int, j: int) -> int:
        """该 (稿件, 评审人) 对当前还能占用的容量：受赛道限额与总容量共同约束。"""
        m = ms[i]
        sink_residual = mf.residual(layout.r_node(j), r_edge_idx[j])
        if m.track is not None and m.track in track_cap[j]:
            return min(
                sink_residual,
                mf.residual(layout.rt_node(j, m.track), rt_edge_idx[(j, m.track)]),
            )
        return sink_residual

    unsatisfied: list[UnsatisfiedManuscript] = []
    unsat_idx: list[int] = []
    for i, m in enumerate(ms):
        assigned = len(assignment[m.id])
        if assigned >= m.required_reviewers:
            continue
        missing = m.required_reviewers - assigned
        elig = eligible[i]
        remaining = sum(_usable_incremental(i, j) for j in elig)
        unsatisfied.append(
            UnsatisfiedManuscript(
                manuscript_id=m.id,
                required=m.required_reviewers,
                assigned=assigned,
                missing=missing,
                eligible_reviewers=len(elig),
                eligible_remaining_capacity=remaining,
                candidate_gap=max(0, m.required_reviewers - len(elig)),
                capacity_gap=max(0, missing - remaining),
            )
        )
        unsat_idx.append(i)

    pool = sorted({j for i in unsat_idx for j in eligible[i]})
    elig_sets = [set(e) for e in eligible]
    shared_pool = {
        "manuscript_ids": [ms[i].id for i in unsat_idx],
        "reviewer_ids": [rs[j].id for j in pool],
        "missing_total": sum(u.missing for u in unsatisfied),
        "pool_remaining_capacity": sum(
            sum(_usable_incremental(i, j) for i in unsat_idx if j in elig_sets[i])
            for j in pool
        ),
        "pool_total_capacity": sum(rs[j].capacity for j in pool),
    }

    return SolveResult(
        feasible=False,
        assignments={},
        total_required=total_required,
        total_assigned=total_assigned,
        unsatisfied=tuple(unsatisfied),
        shared_pool=shared_pool,
    )
