"""集成方式：同一批分支分别给出合并式与变基式可执行顺序。"""

from __future__ import annotations

from .errors import FailureReason, PlanError
from .graph import CommitGraph
from .models import MergePlan, SemVer, Tag
from .planner import ReleasePlanner, _LEVEL_RANK


def _pending_sets(
    graph: CommitGraph, tags: list[Tag], branches: dict[str, str]
) -> tuple[dict[str, str], set[str], set[str]]:
    """校验并计算 (有序分支头, 已发布集合, 待发布集合)。"""
    if not branches:
        raise PlanError(FailureReason.EMPTY_PLAN, "未提供任何分支")
    graph.validate()
    heads: dict[str, str] = {}
    for name, head in sorted(branches.items()):
        if graph.get(head) is None:
            raise PlanError(
                FailureReason.UNKNOWN_BRANCH,
                f"分支 {name!r} 的头提交 {head!r} 不存在",
            )
        heads[name] = head
    released: set[str] = set()
    for t in tags:
        if graph.get(t.commit_id) is None:
            raise PlanError(
                FailureReason.UNKNOWN_TAG,
                f"标签 {t.name!r} 指向的提交 {t.commit_id!r} 不存在",
            )
        released |= graph.ancestors(t.commit_id)
    reachable: set[str] = set()
    for h in heads.values():
        reachable |= graph.ancestors(h)
    return heads, released, reachable - released


def rebase_plan(
    graph: CommitGraph,
    tags: list[Tag],
    branches: dict[str, str],
) -> MergePlan:
    """变基式集成：把分支历史拉成一条直线的执行顺序。

    规则（确定且可解释）：
    - 待发布集合与合并式一致：分支头可达且不属于任何标签祖先集合；
    - 待发布集合中的合并提交（2 个父提交）在变基时被丢弃——其两侧历史
      都会被拉直保留，只有合并点本身消失，记入 skipped 并说明；
    - 八爪鱼合并（>= 3 个父提交）无法拉成语义明确的直线，属于根本
      不适合变基的结构，以 UNREBASEABLE 拒绝（与普通输入错误可区分）；
    - 保留提交按整体拓扑序（父先于子，同层按 id 字典序）拉成直线，
      与传分支、导提交的先后顺序无关；
    - 版本与变更日志只统计被保留（实际会重放）的提交。
    """
    rationale: list[str] = []
    heads, released, pending = _pending_sets(graph, tags, branches)
    rationale.append(
        "参与变基规划的分支: " + ", ".join(f"{n}@{h}" for n, h in heads.items())
    )
    rationale.append(f"待发布 {len(pending)} 个，已发布 {len(released)} 个")

    # 结构检查：八爪鱼合并不可变基
    for cid in sorted(pending):
        c = graph.get(cid)
        if len(c.parents) >= 3:
            raise PlanError(
                FailureReason.UNREBASEABLE,
                f"提交 {cid!r} 是八爪鱼合并（{len(c.parents)} 个父提交），"
                "无法拉成语义明确的直线历史，不适合变基式集成",
            )

    # 普通合并提交：变基时丢弃合并点本身，两侧历史拉直保留
    skipped = sorted(cid for cid in pending if len(graph.get(cid).parents) == 2)
    for cid in skipped:
        rationale.append(
            f"合并提交 {cid!r} 在变基时被丢弃（两侧历史已拉直保留，仅合并点消失）"
        )
    kept = pending - set(skipped)

    full_order = graph.topo_order(list(heads.values()))
    order = [cid for cid in full_order if cid in kept]
    rationale.append(
        f"变基直线顺序共 {len(order)} 个提交（父先于子，同层按 id 字典序），"
        f"跳过 {len(skipped)} 个合并提交"
    )

    changelog = [
        f"{cid}: {graph.get(cid).message or '(无消息)'}" for cid in order
    ]

    # 版本：基线取标签最高者，级别只看实际会重放的保留提交
    base = ReleasePlanner(graph, tags)._baseline(rationale)
    level = "none"
    for cid in order:
        c = graph.get(cid)
        if _LEVEL_RANK.get(c.change, 1) > _LEVEL_RANK[level]:
            level = c.change if c.change in _LEVEL_RANK else "patch"
    if not order:
        rationale.append("无待重放提交，版本保持基线不变")
        next_version = base
    elif base.prerelease:
        next_version = SemVer(base.major, base.minor, base.patch)
        rationale.append(f"基线 {base} 为预发布版本，下一版本毕业为正式版 {next_version}")
    else:
        next_version = base.bump(level)
        rationale.append(f"下一版本: {base} --{level}--> {next_version}")

    return MergePlan(
        order=order,
        next_version=next_version,
        changelog=changelog,
        rationale=rationale,
        skipped=skipped,
    )
