"""集成方式：合并式与变基式顺序都稳定可复现，不可变基结构可区分地失败。"""

import logging

import pytest

from release_planner import Commit, FailureReason, PlanError, Tag
from release_planner.graph import CommitGraph
from release_planner.integration import rebase_plan
from release_planner.planner import ReleasePlanner

log = logging.getLogger(__name__)


def fork_repo() -> tuple[CommitGraph, list[Tag], dict[str, str]]:
    """main: a->b(v1.0.0)->m；feature: b->c->d；m 合并 b 与 d。"""
    g = CommitGraph()
    g.add(Commit("a", message="init", change="major"))
    g.add(Commit("b", parents=("a",), message="release 1.0", change="none"))
    g.add(Commit("c", parents=("b",), branch="feature", message="feat x", change="minor"))
    g.add(Commit("d", parents=("c",), branch="feature", message="fix y", change="patch"))
    g.add(Commit("m", parents=("b", "d"), branch="main", message="merge feature", change="none"))
    tags = [Tag("v1.0.0", "b")]
    branches = {"main": "m", "feature": "d"}
    log.info("输入提交: %s", [(c.id, c.parents, c.change) for c in g.commits()])
    log.info("输入标签: %s, 分支: %s", tags, branches)
    return g, tags, branches


def test_merge_mode_keeps_merge_commit_parents_first():
    g, tags, branches = fork_repo()
    plan = ReleasePlanner(g, tags).plan(branches)
    log.info("合并式顺序: %s", plan.order)
    # 合并点保留，且两个父提交都先于合并提交
    assert plan.order == ["c", "d", "m"]
    assert plan.order.index("d") < plan.order.index("m")
    assert plan.skipped == []  # 合并式不跳过任何提交


def test_rebase_mode_linearizes_and_drops_merge_commit():
    g, tags, branches = fork_repo()
    plan = rebase_plan(g, tags, branches)
    for r in plan.rationale:
        log.info("判定依据: %s", r)
    log.info("变基式顺序: %s, 跳过: %s", plan.order, plan.skipped)
    # 直线历史：合并提交 m 被丢弃，其两侧提交拉直保留
    assert plan.order == ["c", "d"]
    assert plan.skipped == ["m"]
    assert "m" not in plan.order
    # 父先于子仍然成立
    assert plan.order.index("c") < plan.order.index("d")
    # 跳过原因写进了判定依据
    assert any("m" in r and "丢弃" in r for r in plan.rationale)


def test_both_modes_stable_under_input_permutation():
    g, tags, branches = fork_repo()
    merge1 = ReleasePlanner(g, tags).plan(branches)
    rebase1 = rebase_plan(g, tags, branches)

    # 分支传入顺序打乱
    branches_rev = dict(reversed(list(branches.items())))
    merge2 = ReleasePlanner(g, tags).plan(branches_rev)
    rebase2 = rebase_plan(g, tags, branches_rev)

    # 提交导入顺序打乱后重建图
    g2 = CommitGraph()
    for c in reversed(g.commits()):
        g2.add(c)
    merge3 = ReleasePlanner(g2, tags).plan(branches)
    rebase3 = rebase_plan(g2, tags, branches)

    log.info("合并式三种输入: %s / %s / %s", merge1.order, merge2.order, merge3.order)
    log.info("变基式三种输入: %s / %s / %s", rebase1.order, rebase2.order, rebase3.order)
    assert merge1.order == merge2.order == merge3.order
    assert rebase1.order == rebase2.order == rebase3.order
    assert rebase1.skipped == rebase2.skipped == rebase3.skipped
    assert merge1.next_version == merge2.next_version == merge3.next_version
    assert rebase1.next_version == rebase2.next_version == rebase3.next_version


def test_octopus_merge_rejected_with_distinguishable_reason():
    """八爪鱼合并（>=3 父）不可变基，且失败原因与普通输入错误可区分。"""
    g = CommitGraph()
    g.add(Commit("a", message="base", change="none"))
    g.add(Commit("b", parents=("a",), message="side 1", change="patch"))
    g.add(Commit("c", parents=("a",), message="side 2", change="patch"))
    g.add(Commit("o", parents=("a", "b", "c"), message="octopus", change="none"))
    log.info("输入: 八爪鱼合并 o <- (a, b, c)")
    with pytest.raises(PlanError) as ei:
        rebase_plan(g, [], {"main": "o"})
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.UNREBASEABLE
    # 与普通输入错误（如未知分支）不是同一个原因
    assert ei.value.reason is not FailureReason.UNKNOWN_BRANCH
    assert ei.value.reason is not FailureReason.EMPTY_PLAN
    # 同一输入走合并式则完全合法（合并式保留合并点）
    plan = ReleasePlanner(g, []).plan({"main": "o"})
    assert plan.order == ["a", "b", "c", "o"]


def test_rebase_released_merge_commit_not_skipped():
    """已发布历史里的合并提交不在待发布集合内，不参与 skipped。"""
    g, tags, branches = fork_repo()
    # 把合并提交 m 也发布掉，再补一个新提交
    g.add(Commit("n", parents=("m",), message="next", change="patch"))
    tags2 = tags + [Tag("v1.1.0", "m")]
    plan = rebase_plan(g, tags2, {"main": "n"})
    log.info("变基式顺序: %s, 跳过: %s", plan.order, plan.skipped)
    assert plan.order == ["n"]
    assert plan.skipped == []


def test_rebase_version_ignores_dropped_merge_levels():
    """版本提升只看实际会重放的提交。"""
    g = CommitGraph()
    g.add(Commit("a", message="init", change="none"))
    g.add(Commit("b", parents=("a",), message="side", change="patch"))
    # 合并提交自己标了 major，但它会被变基丢弃，不应抬版本
    g.add(Commit("m", parents=("a", "b"), message="merge", change="major"))
    plan = rebase_plan(g, [Tag("v1.0.0", "a")], {"main": "m"})
    log.info("变基式顺序: %s, 跳过: %s, 下一版本: %s", plan.order, plan.skipped, plan.next_version)
    assert plan.skipped == ["m"]
    assert str(plan.next_version) == "1.0.1"  # patch，而非 2.0.0
