"""提交图：导入、校验与查询。"""

from __future__ import annotations

from .errors import FailureReason, PlanError
from .models import Commit


class CommitGraph:
    """提交有向无环图（边由提交指向其父提交）。

    - 重复导入同一提交（id 与内容完全一致）是幂等的，返回 False；
    - id 相同但内容不同视为冲突，抛出 DUPLICATE_COMMIT；
    - validate() 检查父提交缺失与循环父边，分别以可区分原因拒绝。
    """

    def __init__(self) -> None:
        self._commits: dict[str, Commit] = {}

    def add(self, commit: Commit) -> bool:
        """导入提交。返回 True 表示新导入，False 表示幂等跳过。"""
        existing = self._commits.get(commit.id)
        if existing is not None:
            if existing == commit:
                return False
            raise PlanError(
                FailureReason.DUPLICATE_COMMIT,
                f"提交 {commit.id!r} 已存在且内容不一致",
            )
        self._commits[commit.id] = commit
        return True

    def get(self, commit_id: str) -> Commit | None:
        return self._commits.get(commit_id)

    def commits(self) -> list[Commit]:
        return list(self._commits.values())

    def validate(self) -> None:
        """校验父提交缺失与循环父边；任何非法输入都拒绝并给出原因。"""
        for c in self._commits.values():
            for p in c.parents:
                if p not in self._commits:
                    raise PlanError(
                        FailureReason.MISSING_PARENT,
                        f"提交 {c.id!r} 的父提交 {p!r} 缺失",
                    )
        self._check_cycles()

    def _check_cycles(self) -> None:
        """三色标记 DFS 检测父边环。"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {cid: WHITE for cid in self._commits}

        def visit(cid: str, stack: list[str]) -> None:
            color[cid] = GRAY
            stack.append(cid)
            for p in self._commits[cid].parents:
                if color.get(p) == GRAY:
                    cycle = " -> ".join(stack[stack.index(p):] + [p])
                    raise PlanError(
                        FailureReason.CYCLIC_PARENT_EDGE,
                        f"检测到循环父边: {cycle}",
                    )
                if color.get(p) == WHITE:
                    visit(p, stack)
            stack.pop()
            color[cid] = BLACK

        for cid in self._commits:
            if color[cid] == WHITE:
                visit(cid, [])

    def ancestors(self, start: str) -> set[str]:
        """从 start 出发沿父边可达的全部提交（含自身）。调用前需已 validate。"""
        seen: set[str] = set()
        stack = [start]
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            c = self._commits.get(cid)
            if c:
                stack.extend(c.parents)
        return seen

    def merge_base(self, a: str, b: str) -> str | None:
        """a 与 b 的最佳公共祖先（分叉点）。

        取 a 的祖先中同时也是 b 祖先、且其自身不是其他公共祖先祖先的提交；
        若存在多个并列分叉点，按提交 id 字典序取最小者以保证确定性。
        """
        anc_a = self.ancestors(a)
        anc_b = self.ancestors(b)
        common = anc_a & anc_b
        if not common:
            return None
        # 排除“是其他公共祖先的祖先”的提交，留下最深的公共祖先
        deepest = []
        for cid in common:
            if not any(cid != other and cid in self.ancestors(other) for other in common):
                deepest.append(cid)
        return min(deepest) if deepest else None

    def topo_order(self, heads: list[str]) -> list[str]:
        """对 heads 可达的子图做确定性拓扑排序（父先于子）。

        同层候选按提交 id 字典序出队，保证不同操作顺序下结论一致。
        """
        nodes: set[str] = set()
        for h in heads:
            nodes |= self.ancestors(h)
        # 子 -> 父 的入度：每个节点等待其父提交先输出
        children: dict[str, list[str]] = {n: [] for n in nodes}
        indeg: dict[str, int] = {n: 0 for n in nodes}
        for n in nodes:
            for p in self._commits[n].parents:
                if p in nodes:
                    children[p].append(n)
                    indeg[n] += 1
        import heapq

        ready = [n for n in nodes if indeg[n] == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            n = heapq.heappop(ready)
            order.append(n)
            for ch in children[n]:
                indeg[ch] -= 1
                if indeg[ch] == 0:
                    heapq.heappush(ready, ch)
        return order

    def __len__(self) -> int:
        return len(self._commits)
