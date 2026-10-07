"""多发布线：各线独立算版本、无关标签被忽略并说明、跨线总顺序确定。"""

import logging

import pytest

from release_planner import Commit, FailureReason, PlanError, Tag
from release_planner.graph import CommitGraph
from release_planner.lines import plan_lines

log = logging.getLogger(__name__)


def two_line_repo() -> tuple[CommitGraph, list[Tag], dict[str, dict[str, str]]]:
    """两条发布线共享根提交 a（已随 v1.0.0 发布），之后各自分叉。

    main 线:  a -> b(v1.0.0) -> c(minor) -> d(patch)
    hotfix 线: a -> h1(patch) -> h2(major)，且 h1 已随 v0.9.1 发布
    另有一条与本两线无关的 legacy 线: a -> l1，标签 v2.0.0 打在 l1 上。
    """
    g = CommitGraph()
    g.add(Commit("a", message="init", change="none"))
    g.add(Commit("b", parents=("a",), message="release 1.0", change="none"))
    g.add(Commit("c", parents=("b",), message="feat main", change="minor"))
    g.add(Commit("d", parents=("c",), message="fix main", change="patch"))
    g.add(Commit("h1", parents=("a",), message="hotfix 1", change="patch"))
    g.add(Commit("h2", parents=("h1",), message="break api", change="major"))
    g.add(Commit("l1", parents=("a",), message="legacy", change="none"))
    tags = [Tag("v1.0.0", "b"), Tag("v0.9.1", "h1"), Tag("v2.0.0", "l1")]
    lines = {
        "main": {"main": "d"},
        "hotfix": {"hf": "h2"},
    }
    log.info("输入提交: %s", [(c.id, c.parents, c.change) for c in g.commits()])
    log.info("输入标签: %s", [(t.name, t.commit_id) for t in tags])
    log.info("发布线: %s", lines)
    return g, tags, lines


def test_each_line_computes_its_own_version():
    g, tags, lines = two_line_repo()
    result = plan_lines(g, tags, lines)
    for name, lp in result.lines.items():
        log.info("线 %s: order=%s next=%s ignored=%s",
                 name, lp.plan.order, lp.plan.next_version, lp.ignored_tags)
    main = result.lines["main"]
    hotfix = result.lines["hotfix"]
    # main 线基线只看 v1.0.0（v2.0.0 在别的线上，不得抬高本线版本）
    assert str(main.plan.next_version) == "1.1.0"
    assert main.plan.order == ["c", "d"]
    # hotfix 线基线只看 v0.9.1，h2 是 major
    assert str(hotfix.plan.next_version) == "1.0.0"
    assert hotfix.plan.order == ["h2"]


def test_unrelated_tags_ignored_and_explained():
    g, tags, lines = two_line_repo()
    result = plan_lines(g, tags, lines)
    main = result.lines["main"]
    # v2.0.0 与 v0.9.1 都不在 main 线可达历史内，必须被忽略并说明
    assert any("v2.0.0" in s for s in main.ignored_tags)
    assert any("v0.9.1" in s for s in main.ignored_tags)
    assert not any("v1.0.0" in s for s in main.ignored_tags)
    for s in main.ignored_tags:
        log.info("main 线忽略说明: %s", s)
    # 忽略原因要出现在整体 rationale 里
    assert any("忽略" in r and "v2.0.0" in r for r in result.rationale)


def test_global_order_shared_released_and_deterministic():
    g, tags, lines = two_line_repo()
    r1 = plan_lines(g, tags, lines)
    # 传线、传分支的顺序打乱后结论必须一致
    lines_rev = dict(reversed(list(lines.items())))
    lines_rev["main"] = dict(reversed(list(lines_rev["main"].items())))
    r2 = plan_lines(g, tags, lines_rev)
    log.info("正序总顺序: %s / 逆序总顺序: %s", r1.global_order, r2.global_order)
    assert r1.global_order == r2.global_order
    # 总顺序 = 各线待发布并集；共享的已发布部分（a、b、h1）不出现
    assert sorted(r1.global_order) == ["c", "d", "h2"]
    assert "a" not in r1.global_order and "b" not in r1.global_order
    # 父先于子
    assert r1.global_order.index("c") < r1.global_order.index("d")


def test_global_order_independent_of_commit_import_order():
    g, tags, lines = two_line_repo()
    r1 = plan_lines(g, tags, lines)
    # 同一批提交换个导入顺序重建图
    g2 = CommitGraph()
    for c in reversed(g.commits()):
        g2.add(c)
    r2 = plan_lines(g2, tags, lines)
    log.info("导入顺序变化前后总顺序: %s / %s", r1.global_order, r2.global_order)
    assert r1.global_order == r2.global_order
    for name in r1.lines:
        assert r1.lines[name].plan.order == r2.lines[name].plan.order
        assert r1.lines[name].plan.next_version == r2.lines[name].plan.next_version


def test_tag_on_shared_commit_counts_for_both_lines():
    """共享已发布部分上的标签对两条线都是有效基线。"""
    g, tags, lines = two_line_repo()
    # v1.0.0 打在 b 上，b 只在 main 线；a 是两线共享且随 v1.0.0 发布
    result = plan_lines(g, tags, lines)
    # hotfix 线的 h1 祖先含 a，但 v1.0.0 指向 b，b 不在 hotfix 线上
    assert any("v1.0.0" in s for s in result.lines["hotfix"].ignored_tags)
    # a 已被 v0.9.1 覆盖（h1 的祖先），不会出现在 hotfix 待发布里
    assert "a" not in result.lines["hotfix"].plan.order


def test_empty_lines_rejected():
    g, tags, _ = two_line_repo()
    with pytest.raises(PlanError) as ei:
        plan_lines(g, tags, {})
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.EMPTY_PLAN


def test_unknown_head_in_line_rejected():
    g, tags, _ = two_line_repo()
    with pytest.raises(PlanError) as ei:
        plan_lines(g, tags, {"main": {"main": "ghost"}})
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.UNKNOWN_BRANCH
