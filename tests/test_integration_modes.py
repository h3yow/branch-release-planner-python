"""合并式 / 变基式集成：两套顺序稳定可复现，不可变基结构显式失败。

判定规则：
- 合并式：保留合并点，父必先于子；同层按提交 id 字典序。
- 变基式：待发布区间内的合并提交 skip（合并点不再保留，其各父分支上的
  非合并提交按拓扑序重放），非合并提交 keep，最终拉成一条直线。
- 两套顺序都与分支传入顺序、提交导入顺序无关。
- 邪恶合并（携带语义变更）、无公共祖先的合并、章鱼合并（>2 父）不适合变基，
  必须以 rebase_unsupported 失败，区别于 missing_parent 等普通输入错误。
"""

import logging

import pytest

from release_planner import Commit, FailureReason, PlanError, Tag
from release_planner.graph import CommitGraph
from release_planner.integration import MultiLinePlanner
from release_planner.models import IntegrationMode

log = logging.getLogger(__name__)


def fork_with_merge() -> tuple[CommitGraph, list[Tag], dict[str, str]]:
    """a->b(tag v1.0.0)->m(merge b,c)；feat: b->c。"""
    g = CommitGraph()
    for c in [
        Commit("a", message="init", change="major"),
        Commit("b", parents=("a",), message="release 1.0", change="none"),
        Commit("c", parents=("b",), branch="feat", message="feat", change="minor"),
        Commit("m", parents=("b", "c"), branch="main", message="merge feat", change="none"),
    ]:
        g.add(c)
    return g, [Tag("v1.0.0", "b")], {"main": "m", "feat": "c"}


def test_merge_order_keeps_merge_commit():
    g, tags, branches = fork_with_merge()
    plan = MultiLinePlanner(g, tags).plan_merge(branches)
    log.info("合并式总顺序: %s（合并点 m 必须保留且在父之后）", plan.merge_order)
    assert plan.merge_order == ["c", "m"]
    assert "m" in plan.merge_order


def test_rebase_linearizes_and_skips_merge():
    g, tags, branches = fork_with_merge()
    rp = MultiLinePlanner(g, tags).plan_rebase(branches)
    log.info("变基式总顺序: %s", rp.order)
    log.info("跳过: %s", [(s.commit_id, s.reason) for s in rp.skipped])
    # 合并提交 m 被跳过，直线上只剩 c
    assert rp.order == ["c"]
    assert "m" not in rp.order
    main_steps = [(s.action, s.commit_id) for s in rp.lines["main"]]
    log.info("main 线变基步骤: %s", main_steps)
    assert main_steps == [("keep", "c"), ("skip", "m")]
    assert all(s.action == "skip" for s in rp.skipped)
    assert "合并提交" in rp.skipped[0].reason


def test_both_orders_independent_of_input_order():
    g, tags, branches = fork_with_merge()
    planner = MultiLinePlanner(g, tags)
    m1 = planner.plan_merge(branches)
    m2 = planner.plan_merge(dict(reversed(list(branches.items()))))
    r1 = planner.plan_rebase(branches)
    r2 = planner.plan_rebase(dict(reversed(list(branches.items()))))
    log.info("合并式 正序=%s 逆序=%s", m1.merge_order, m2.merge_order)
    log.info("变基式 正序=%s 逆序=%s", r1.order, r2.order)
    assert m1.merge_order == m2.merge_order
    assert r1.order == r2.order


def test_plan_all_contains_both_orders():
    g, tags, branches = fork_with_merge()
    p = MultiLinePlanner(g, tags).plan_all(branches)
    log.info("plan_all: merge=%s rebase=%s", p.merge_order, p.rebase_order)
    assert p.merge_order == ["c", "m"]
    assert p.rebase_order == ["c"]
    assert p.rebase is not None and p.rebase.order == ["c"]


def test_mode_dispatch():
    g, tags, branches = fork_with_merge()
    planner = MultiLinePlanner(g, tags)
    assert planner.plan(branches, IntegrationMode.MERGE).merge_order == ["c", "m"]
    assert planner.plan(branches, IntegrationMode.REBASE).order == ["c"]


def evil_merge_repo() -> tuple[CommitGraph, list[Tag], dict[str, str]]:
    """与 fork 相同拓扑，但合并提交 m 携带 patch 语义变更（邪恶合并）。"""
    g = CommitGraph()
    for c in [
        Commit("a", message="init", change="major"),
        Commit("b", parents=("a",), message="release 1.0", change="none"),
        Commit("c", parents=("b",), branch="feat", message="feat", change="minor"),
        Commit("m", parents=("b", "c"), branch="main",
               message="evil merge fix", change="patch"),
    ]:
        g.add(c)
    return g, [Tag("v1.0.0", "b")], {"main": "m", "feat": "c"}


def test_evil_merge_rejected_for_rebase():
    """合并提交携带了语义变更（change != none）：变基跳过合并点会丢变更。"""
    g, tags, branches = evil_merge_repo()
    with pytest.raises(PlanError) as ei:
        MultiLinePlanner(g, tags).plan_rebase(branches)
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.REBASE_UNSUPPORTED
    assert "邪恶合并" in ei.value.detail
    # 合并式仍可正常给出顺序
    assert MultiLinePlanner(g, tags).plan_merge(branches).merge_order == ["c", "m"]


def test_unrelated_history_merge_rejected_for_rebase():
    """合并两条无公共祖先的历史：变基无法表达。"""
    g = CommitGraph()
    g.add(Commit("r0", message="root0", change="none"))
    g.add(Commit("s0", message="root1", change="none"))
    g.add(Commit("mm", parents=("r0", "s0"), branch="main",
                 message="merge unrelated", change="none"))
    log.info("输入: mm 的两个父 r0/s0 无公共祖先")
    with pytest.raises(PlanError) as ei:
        MultiLinePlanner(g, []).plan_rebase({"main": "mm"})
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.REBASE_UNSUPPORTED
    assert "无公共祖先" in ei.value.detail


def test_octopus_merge_rejected_for_rebase():
    """三父合并（章鱼合并）不适合拉成直线。"""
    g = CommitGraph()
    g.add(Commit("a", message="init", change="none"))
    g.add(Commit("x", parents=("a",), branch="f1", message="f1", change="patch"))
    g.add(Commit("y", parents=("a",), branch="f2", message="f2", change="patch"))
    g.add(Commit("o", parents=("a", "x", "y"), branch="main",
                 message="octopus", change="none"))
    with pytest.raises(PlanError) as ei:
        MultiLinePlanner(g, []).plan_rebase({"main": "o"})
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.REBASE_UNSUPPORTED
    assert "章鱼合并" in ei.value.detail


def test_rebase_unsupported_distinct_from_input_errors():
    """不可变基与普通输入错误可区分（reason 不同），且不返回假顺序。"""
    # 普通输入错误：缺失父提交
    g = CommitGraph()
    g.add(Commit("p", parents=("ghost",)))
    with pytest.raises(PlanError) as ei:
        MultiLinePlanner(g, []).plan_rebase({"main": "p"})
    log.info("输入错误原因: %s", ei.value.reason)
    assert ei.value.reason is FailureReason.MISSING_PARENT

    # 不可变基：邪恶合并
    g2, tags2, branches2 = evil_merge_repo()
    with pytest.raises(PlanError) as ei2:
        MultiLinePlanner(g2, tags2).plan_rebase(branches2)
    assert ei2.value.reason is FailureReason.REBASE_UNSUPPORTED
    assert ei2.value.reason is not FailureReason.MISSING_PARENT
