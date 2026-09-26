"""提交图：分叉合并、循环父边、缺失父提交、重复导入。"""

import logging

import pytest

from release_planner import Commit, FailureReason, PlanError
from release_planner.graph import CommitGraph

log = logging.getLogger(__name__)


def build_fork_graph() -> CommitGraph:
    """main: a->b->m；feature: b->c->d；m 为合并提交 (b, d)。"""
    g = CommitGraph()
    commits = [
        Commit("a", message="init"),
        Commit("b", parents=("a",), message="base"),
        Commit("c", parents=("b",), branch="feature", message="feat"),
        Commit("d", parents=("c",), branch="feature", message="fix"),
        Commit("m", parents=("b", "d"), branch="main", message="merge"),
    ]
    for c in commits:
        g.add(c)
    log.info("输入提交: %s", [(c.id, c.parents) for c in commits])
    return g


def test_fork_merge_detected():
    g = build_fork_graph()
    g.validate()
    base = g.merge_base("m", "d")
    log.info("分叉点判定: merge_base(m, d) = %r（d 本身是 m 的第二父）", base)
    assert base == "d"
    # c 已包含在 m 中（c 是 m 的祖先），分叉点即 c 自身
    base2 = g.merge_base("c", "m")
    log.info("分叉点判定: merge_base(c, m) = %r（c 已被 m 包含）", base2)
    assert base2 == "c"
    # 真正的分叉：e 与 c 都从 b 出发且互不包含
    g.add(Commit("e", parents=("b",), branch="main", message="main work"))
    base3 = g.merge_base("c", "e")
    log.info("分叉点判定: merge_base(c, e) = %r（共同祖先 b）", base3)
    assert base3 == "b"
    order = g.topo_order(["m"])
    log.info("拓扑序: %s（父必先于子，合并提交 m 最后）", order)
    assert order == ["a", "b", "c", "d", "m"]


def test_cyclic_parent_edge_rejected():
    g = CommitGraph()
    g.add(Commit("x", parents=("y",)))
    g.add(Commit("y", parents=("x",)))
    log.info("输入: x.parents=[y], y.parents=[x]（构成环）")
    with pytest.raises(PlanError) as ei:
        g.validate()
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.CYCLIC_PARENT_EDGE


def test_missing_parent_rejected():
    g = CommitGraph()
    g.add(Commit("p", parents=("ghost",)))
    log.info("输入: p.parents=[ghost]，ghost 未导入")
    with pytest.raises(PlanError) as ei:
        g.validate()
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.MISSING_PARENT


def test_duplicate_import_idempotent_and_conflict_distinguished():
    g = CommitGraph()
    assert g.add(Commit("a", message="v1")) is True
    assert g.add(Commit("a", message="v1")) is False  # 完全重复：幂等跳过
    assert len(g) == 1
    log.info("重复导入同一提交被幂等跳过，图中仍只有 1 个提交")
    with pytest.raises(PlanError) as ei:
        g.add(Commit("a", message="v2"))  # id 相同内容不同：冲突
    log.info("冲突拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.DUPLICATE_COMMIT
