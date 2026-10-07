"""多发布线编排：每线独立版本基线、合并式与变基式两套可执行顺序。"""

from __future__ import annotations

from .errors import FailureReason, PlanError
from .graph import CommitGraph
from .models import (
    IgnoredTag,
    IntegrationMode,
    LinePlan,
    MultiReleasePlan,
    RebasePlan,
    RebaseStep,
    SemVer,
    Tag,
)

_LEVEL_RANK = {"none": 0, "patch": 1, "minor": 2, "major": 3}


class MultiLinePlanner:
    """对一批目标分支同时编排多条发布线。

    版本基线只看各发布线自己历史上的标签：标签指向的提交必须是该线头提交的
    祖先才参与该线的版本排序；不属于该线的标签不影响该线版本号，但会以
    ``ignored_tags`` 明确列出忽略原因。

    合并式顺序保留合并点，只要求父先于子；变基式顺序跳过待发布区间内的合并
    提交、把历史拉成直线。两套顺序都基于确定性拓扑序（同层按提交 id 字典序），
    与分支传入顺序、提交导入顺序无关。
    """

    def __init__(self, graph: CommitGraph, tags: list[Tag] | None = None) -> None:
        self._graph = graph
        self._tags = sorted(tags or [], key=lambda t: t.name)

    # ---- 输入校验 ----

    def _validate(self, branches: dict[str, str]) -> dict[str, str]:
        if not branches:
            raise PlanError(FailureReason.EMPTY_PLAN, "未提供任何分支")
        self._graph.validate()
        heads: dict[str, str] = {}
        for name in sorted(branches):
            head = branches[name]
            if self._graph.get(head) is None:
                raise PlanError(
                    FailureReason.UNKNOWN_BRANCH,
                    f"分支 {name!r} 的头提交 {head!r} 不存在",
                )
            heads[name] = head
        for t in self._tags:
            if self._graph.get(t.commit_id) is None:
                raise PlanError(
                    FailureReason.UNKNOWN_TAG,
                    f"标签 {t.name!r} 指向的提交 {t.commit_id!r} 不存在",
                )
        return heads

    # ---- 单线分析：版本基线与待发布集合 ----

    def _analyze_line(self, name: str, head: str) -> tuple[LinePlan, set[str]]:
        ancestors = self._graph.ancestors(head)

        line_tags: list[tuple[SemVer, Tag]] = []
        ignored: list[IgnoredTag] = []
        for t in self._tags:
            if t.commit_id in ancestors:
                line_tags.append((SemVer.parse(t.name), t))
            else:
                ignored.append(
                    IgnoredTag(
                        name=t.name,
                        commit_id=t.commit_id,
                        reason=(
                            f"标签 {t.name!r} 指向的提交 {t.commit_id!r} 不在发布线 "
                            f"{name!r}（头 {head!r}）的祖先历史中，不参与该线版本基线"
                        ),
                    )
                )
        line_tags.sort(key=lambda x: x[0])

        released: set[str] = set()
        for _, t in line_tags:
            released |= self._graph.ancestors(t.commit_id)
        pending_set = ancestors - released
        order = [c for c in self._graph.topo_order([head]) if c in pending_set]

        if line_tags:
            base_v, base_t = line_tags[-1]
            baseline_tag = base_t.name
            baseline = base_v
        else:
            baseline_tag = None
            baseline = SemVer(0, 0, 0)

        level = "none"
        for cid in order:
            c = self._graph.get(cid)
            if _LEVEL_RANK.get(c.change, 1) > _LEVEL_RANK[level]:
                level = c.change if c.change in _LEVEL_RANK else "patch"

        if not order:
            next_version = baseline
        elif baseline.prerelease:
            next_version = SemVer(baseline.major, baseline.minor, baseline.patch)
        else:
            next_version = baseline.bump(level)

        changelog = [
            f"{cid}: {self._graph.get(cid).message or '(无消息)'}" for cid in order
        ]

        line = LinePlan(
            branch=name,
            head=head,
            next_version=next_version,
            baseline=baseline,
            baseline_tag=baseline_tag,
            changelog=changelog,
            used_tags=[t.name for _, t in line_tags],
            ignored_tags=ignored,
            pending=order,
        )
        return line, pending_set

    # ---- 合并式集成 ----

    def plan_merge(self, branches: dict[str, str]) -> MultiReleasePlan:
        heads = self._validate(branches)
        names = sorted(heads)

        lines: list[LinePlan] = []
        pending_by_line: dict[str, set[str]] = {}
        all_pending: set[str] = set()
        for name in names:
            line, pending = self._analyze_line(name, heads[name])
            lines.append(line)
            pending_by_line[name] = pending
            all_pending |= pending

        heads_in_order = [heads[n] for n in names]
        merge_order = [
            cid for cid in self._graph.topo_order(heads_in_order) if cid in all_pending
        ]

        counts: dict[str, int] = {}
        for pending in pending_by_line.values():
            for cid in pending:
                counts[cid] = counts.get(cid, 0) + 1
        shared = sorted(cid for cid, n in counts.items() if n > 1)

        rationale: list[str] = []
        rationale.append(
            "参与编排的发布线（按分支名排序）: "
            + ", ".join(f"{n}@{heads[n]}" for n in names)
        )
        rationale.append(
            "合并式总顺序取各线待发布提交并集的拓扑序（父先于子，合并点保留），"
            "同层按提交 id 字典序；结果与分支传入、提交导入顺序无关"
        )
        if shared:
            rationale.append(
                f"{len(shared)} 个已发布段被多条线共用，在总顺序中只出现一次: "
                + ", ".join(shared)
            )
        for line in lines:
            if line.ignored_tags:
                rationale.append(
                    f"发布线 {line.branch!r} 忽略 {len(line.ignored_tags)} 个"
                    "不属于其历史的标签: "
                    + ", ".join(t.name for t in line.ignored_tags)
                )

        return MultiReleasePlan(
            lines=lines,
            merge_order=merge_order,
            rebase_order=[],
            rebase=None,
            shared_commits=shared,
            rationale=rationale,
        )

    # ---- 变基式集成 ----

    def _rebase_problems(self, names: list[str], heads: dict[str, str]) -> dict[str, set[str]]:
        """返回每条线的待发布集合，同时收集不适合变基的结构。"""
        pending_by_line: dict[str, set[str]] = {}
        problems: list[str] = []
        for name in names:
            _, pending = self._analyze_line(name, heads[name])
            pending_by_line[name] = pending
            for cid in sorted(pending):
                c = self._graph.get(cid)
                if len(c.parents) < 2:
                    continue
                if len(c.parents) > 2:
                    problems.append(
                        f"发布线 {name!r} 的合并提交 {cid!r} 有 {len(c.parents)} 个"
                        "父提交（章鱼合并），无法安全拉成直线"
                    )
                    continue
                p1, p2 = c.parents
                common = self._graph.ancestors(p1) & self._graph.ancestors(p2)
                if not common:
                    problems.append(
                        f"发布线 {name!r} 的合并提交 {cid!r} 合并了两条无公共祖先的"
                        "历史，变基无法表达该合并点"
                    )
                if c.change != "none":
                    problems.append(
                        f"发布线 {name!r} 的合并提交 {cid!r} 携带了语义变更"
                        f"（change={c.change!r}，邪恶合并），变基跳过合并点会丢失该变更"
                    )
        if problems:
            raise PlanError(
                FailureReason.REBASE_UNSUPPORTED,
                "以下结构不适合变基: " + "；".join(problems),
            )
        return pending_by_line

    def plan_rebase(self, branches: dict[str, str]) -> RebasePlan:
        heads = self._validate(branches)
        names = sorted(heads)
        pending_by_line = self._rebase_problems(names, heads)

        steps_by_line: dict[str, list[RebaseStep]] = {}
        all_kept: set[str] = set()
        skipped: list[RebaseStep] = []
        for name in names:
            steps: list[RebaseStep] = []
            line_pending = pending_by_line[name]
            for cid in self._graph.topo_order([heads[name]]):
                if cid not in line_pending:
                    continue
                c = self._graph.get(cid)
                if len(c.parents) >= 2:
                    step = RebaseStep(
                        action="skip",
                        commit_id=cid,
                        reason=(
                            f"合并提交 {cid!r} 在变基时跳过：其变更已由各父分支上的"
                            "非合并提交按线性顺序重放，合并点不再保留"
                        ),
                        branch=name,
                    )
                    skipped.append(step)
                else:
                    step = RebaseStep(action="keep", commit_id=cid, branch=name)
                    all_kept.add(cid)
                steps.append(step)
            steps_by_line[name] = steps

        heads_in_order = [heads[n] for n in names]
        order = [
            cid for cid in self._graph.topo_order(heads_in_order) if cid in all_kept
        ]

        rationale: list[str] = []
        rationale.append(
            "变基式顺序把每条发布线待发布区间内的非合并提交按拓扑序（父先于子，"
            "同层按提交 id 字典序）拉成直线；合并提交一律 skip，不保留合并点"
        )
        if skipped:
            rationale.append(
                f"共跳过 {len(skipped)} 个合并提交: "
                + ", ".join(s.commit_id for s in skipped)
            )
        rationale.append(
            "多条线共用的提交在总线性顺序中只出现一次；顺序与分支传入、提交导入"
            "顺序无关"
        )

        return RebasePlan(
            order=order,
            lines=steps_by_line,
            skipped=skipped,
            rationale=rationale,
        )

    def plan_all(self, branches: dict[str, str]) -> MultiReleasePlan:
        """同时给出合并式与变基式结论；结构不适合变基时抛 REBASE_UNSUPPORTED。"""
        merge_plan = self.plan_merge(branches)
        rebase_plan = self.plan_rebase(branches)
        merge_plan.rebase = rebase_plan
        merge_plan.rebase_order = rebase_plan.order
        return merge_plan

    def plan(
        self, branches: dict[str, str], mode: IntegrationMode = IntegrationMode.MERGE
    ):
        """按指定集成方式规划。"""
        if mode is IntegrationMode.REBASE:
            return self.plan_rebase(branches)
        return self.plan_merge(branches)
