"""多发布线：各线独立版本基线、无关标签忽略、跨线总顺序稳定、共用提交去重。

判定规则（每组用例都在日志中说明输入与规则）：
- 某条线的版本基线只看“标签提交是该线头祖先”的标签，取 semver 最高者；
- 不属于该线的标签不影响版本号，但必须出现在 ignored_tags 中并说明原因；
- 跨线总顺序为各线待发布提交并集的确定性拓扑序（父先于子，同层 id 字典序），
  与分支传入顺序、提交导入顺序无关；多线共用提交只出现一次。
"""

import logging

from release_planner import Commit, Tag
from release_planner.integration import MultiLinePlanner
from release_planner.graph import CommitGraph

log = logging.getLogger(__name__)


def two_lines_repo() -> tuple[CommitGraph, list[Tag], dict[str, str]]:
    """main: a->b->m(merge feat)->p；rel2: b->r1->r2。

    标签 v1.0.0 在共同基点 b 上（两线都认），v2.0.0 在 rel2 的 r1 上
    （main 线历史里没有，必须被 main 忽略、不能抬高 main 的版本号）。
    """
    g = CommitGraph()
    for c in [
        Commit("a", message="init", change="major"),
        Commit("b", parents=("a",), message="release 1.0", change="none"),
        Commit("c", parents=("b",), branch="feat", message="feat x", change="minor"),
        Commit("m", parents=("b", "c"), branch="main", message="merge feat", change="none"),
        Commit("p", parents=("m",), branch="main", message="main patch", change="patch"),
        Commit("r1", parents=("b",), branch="rel2", message="rel2 feature", change="minor"),
        Commit("r2", parents=("r1",), branch="rel2", message="rel2 patch", change="patch"),
    ]:
        g.add(c)
    tags = [Tag("v1.0.0", "b"), Tag("v2.0.0", "r1")]
    branches = {"main": "p", "rel2": "r2"}
    log.info("输入提交: %s", [(c.id, c.parents, c.change) for c in g.commits()])
    log.info("输入标签: %s；分支: %s", [(t.name, t.commit_id) for t in tags], branches)
    return g, tags, branches


def test_each_line_uses_its_own_tag_baseline():
    g, tags, branches = two_lines_repo()
    plan = MultiLinePlanner(g, tags).plan_merge(branches)
    by = {line.branch: line for line in plan.lines}
    log.info("main: 基线=%s 下一版=%s 使用标签=%s",
             by["main"].baseline_tag, by["main"].next_version, by["main"].used_tags)
    log.info("rel2: 基线=%s 下一版=%s 使用标签=%s",
             by["rel2"].baseline_tag, by["rel2"].next_version, by["rel2"].used_tags)
    # main 只认 v1.0.0，minor/patch 待发布 -> 1.1.0
    assert by["main"].baseline_tag == "v1.0.0"
    assert str(by["main"].next_version) == "1.1.0"
    assert by["main"].used_tags == ["v1.0.0"]
    # rel2 认 v2.0.0，r2 是 patch -> 2.0.1
    assert by["rel2"].baseline_tag == "v2.0.0"
    assert str(by["rel2"].next_version) == "2.0.1"


def test_unrelated_tags_ignored_and_explained():
    g, tags, branches = two_lines_repo()
    plan = MultiLinePlanner(g, tags).plan_merge(branches)
    by = {line.branch: line for line in plan.lines}
    ignored_main = [(t.name, t.commit_id) for t in by["main"].ignored_tags]
    log.info("main 忽略的标签: %s", ignored_main)
    for t in by["main"].ignored_tags:
        log.info("忽略原因: %s", t.reason)
    assert ("v2.0.0", "r1") in ignored_main
    assert all("不参与该线版本基线" in t.reason for t in by["main"].ignored_tags)
    # rel2 历史包含共同基点 b，两个标签都认，无忽略
    assert by["rel2"].ignored_tags == []


def test_per_line_changelog_and_pending():
    g, tags, branches = two_lines_repo()
    plan = MultiLinePlanner(g, tags).plan_merge(branches)
    by = {line.branch: line for line in plan.lines}
    log.info("main 待发布: %s / changelog: %s", by["main"].pending, by["main"].changelog)
    log.info("rel2 待发布: %s / changelog: %s", by["rel2"].pending, by["rel2"].changelog)
    assert by["main"].pending == ["c", "m", "p"]
    assert by["main"].changelog == [
        "c: feat x", "m: merge feat", "p: main patch",
    ]
    assert by["rel2"].pending == ["r2"]  # r1 已被 v2.0.0 发布


def test_cross_line_total_order_stable_and_deduped():
    g, tags, branches = two_lines_repo()
    planner = MultiLinePlanner(g, tags)
    p1 = planner.plan_merge(branches)
    p2 = planner.plan_merge(dict(reversed(list(branches.items()))))
    log.info("正序总顺序: %s", p1.merge_order)
    log.info("逆序总顺序: %s", p2.merge_order)
    assert p1.merge_order == p2.merge_order
    # 父先于子，合并点保留；顺序唯一确定
    assert p1.merge_order == ["c", "m", "p", "r2"]
    # 没有共用提交（两线在 b 之后分叉，b 已发布）
    assert p1.shared_commits == []


def test_shared_released_segment_appears_once():
    """main 与 hotfix 都包含一段未打标签的共用提交 x，总顺序里 x 只出现一次。"""
    g = CommitGraph()
    for c in [
        Commit("a", message="init", change="major"),
        Commit("x", parents=("a",), message="shared work", change="minor"),
        Commit("u", parents=("x",), branch="main", message="main work", change="patch"),
        Commit("v", parents=("x",), branch="hotfix", message="hotfix work", change="patch"),
    ]:
        g.add(c)
    tags = [Tag("v0.9.0", "a")]  # a 已发布，共用的待发布段只有 x
    branches = {"hotfix": "v", "main": "u"}
    log.info("输入: 共用提交 x 的两个子 u/v，无标签")
    plan = MultiLinePlanner(g, tags).plan_merge(branches)
    log.info("总顺序: %s, 共用提交: %s", plan.merge_order, plan.shared_commits)
    assert plan.merge_order.count("x") == 1
    assert plan.shared_commits == ["x"]
    # 与传入顺序无关
    plan2 = MultiLinePlanner(g, tags).plan_merge({"main": "u", "hotfix": "v"})
    assert plan2.merge_order == plan.merge_order
