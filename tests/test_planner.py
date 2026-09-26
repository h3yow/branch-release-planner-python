"""规划器：分叉合并顺序、预发布排序、重复导入幂等、操作顺序无关。"""

import logging

import pytest

from release_planner import Commit, FailureReason, PlanError, SemVer, Tag
from release_planner.graph import CommitGraph
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


def test_fork_merge_plan():
    g, tags, branches = fork_repo()
    plan = ReleasePlanner(g, tags).plan(branches)
    for r in plan.rationale:
        log.info("判定依据: %s", r)
    log.info("合并顺序: %s, 下一版本: %s", plan.order, plan.next_version)
    assert plan.order == ["c", "d", "m"]       # 父先于子，合并提交最后
    assert str(plan.next_version) == "1.1.0"   # 最高级别 minor
    assert plan.changelog == ["c: feat x", "d: fix y", "m: merge feature"]


def test_plan_independent_of_branch_input_order():
    g, tags, branches = fork_repo()
    plan1 = ReleasePlanner(g, tags).plan(branches)
    plan2 = ReleasePlanner(g, tags).plan(dict(reversed(list(branches.items()))))
    log.info("正序: %s / 逆序: %s", plan1.order, plan2.order)
    assert plan1.order == plan2.order
    assert plan1.next_version == plan2.next_version


def test_prerelease_and_build_metadata_ordering():
    g = CommitGraph()
    g.add(Commit("a", message="init", change="none"))
    g.add(Commit("n", parents=("a",), message="fix", change="patch"))
    tags = [
        Tag("v1.0.0-alpha", "a"),
        Tag("v1.0.0-alpha.1", "a"),
        Tag("v1.0.0-alpha.beta", "a"),
        Tag("v1.0.0-beta", "a"),
        Tag("v1.0.0-beta.2", "a"),
        Tag("v1.0.0-beta.11", "a"),
        Tag("v1.0.0-rc.1", "a"),
        Tag("v1.0.0-rc.1+build.99", "a"),  # 构建元数据不影响排序
    ]
    log.info("输入标签: %s", [t.name for t in tags])
    plan = ReleasePlanner(g, tags).plan({"main": "n"})
    for r in plan.rationale:
        log.info("判定依据: %s", r)
    # 最高标签为 1.0.0-rc.1（build 元数据不参与比较），patch 提升 -> 1.0.0
    assert str(plan.next_version) == "1.0.0"


def test_duplicate_import_not_double_counted():
    g, tags, branches = fork_repo()
    # 重复导入同一提交（幂等），再规划
    assert g.add(Commit("c", parents=("b",), branch="feature", message="feat x", change="minor")) is False
    plan = ReleasePlanner(g, tags).plan(branches)
    log.info("重复导入后日志: %s", plan.changelog)
    assert plan.changelog.count("c: feat x") == 1
    assert str(plan.next_version) == "1.1.0"  # 不会因重复导入而再次提升


def test_unknown_branch_rejected():
    g, tags, _ = fork_repo()
    with pytest.raises(PlanError) as ei:
        ReleasePlanner(g, tags).plan({"main": "ghost"})
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.UNKNOWN_BRANCH


def test_empty_plan_rejected():
    g, tags, _ = fork_repo()
    with pytest.raises(PlanError) as ei:
        ReleasePlanner(g, tags).plan({})
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.EMPTY_PLAN


def test_invalid_version_tag_rejected():
    g = CommitGraph()
    g.add(Commit("a"))
    with pytest.raises(PlanError) as ei:
        ReleasePlanner(g, [Tag("not-a-version", "a")]).plan({"main": "a"})
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.INVALID_VERSION


def test_no_pending_keeps_baseline():
    g, tags, branches = fork_repo()
    # 所有提交都已发布：给 m 打上更高标签
    tags2 = tags + [Tag("v1.1.0", "m")]
    plan = ReleasePlanner(g, tags2).plan(branches)
    log.info("判定依据: %s", plan.rationale)
    assert plan.order == []
    assert str(plan.next_version) == "1.1.0"
