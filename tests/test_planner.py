"""Merge ordering determinism, release bumps, prerelease priority, dedup changelog."""

import logging

import pytest

from branch_release_planner.errors import InvalidRefError, NoBaseVersionError
from branch_release_planner.graph import CommitGraph
from branch_release_planner.models import Branch, Tag
from branch_release_planner.planner import MergePlanner

from .conftest import B, C, T, log_case, log_reasons

LOG = logging.getLogger("planner-tests")


def _forked_repo():
    # 0(tag v1.0.0) -- 1 fix -- 2 feat            main -> 2
    #       \------ 3 fix (topic-a, old ts)
    #       \------ 4 fix (topic-b, new ts)
    commits = [
        C("0", (), "init", 0),
        C("1", ("0",), "fix: patch bug", 1),
        C("2", ("1",), "feat: new api", 2),
        C("3", ("0",), "fix: topic-a", 1),
        C("4", ("0",), "fix: topic-b", 4),
    ]
    g = CommitGraph().import_commits(commits)
    g.validate()
    branches = {
        "main": B("main", "2"),
        "topic-a": B("topic-a", "3"),
        "topic-b": B("topic-b", "4"),
    }
    tags = {"v1.0.0": T("v1.0.0", "0")}
    return g, branches, tags


def test_merge_order_independent_of_input_order():
    g, branches, tags = _forked_repo()
    planner = MergePlanner(g, branches, tags)
    log_case(
        "merge-order",
        heads={n: b.head for n, b in branches.items()},
        inputs=[["topic-a", "topic-b"], ["topic-b", "topic-a"]],
    )
    o1 = [s.source_branch for s in planner.plan_merge_order("main", ["topic-a", "topic-b"])]
    o2 = [s.source_branch for s in planner.plan_merge_order("main", ["topic-b", "topic-a"])]
    assert o1 == o2 == ["topic-b", "topic-a"]  # equal depth: newer head first, then name
    LOG.info("DECISION: both input orders -> %s (tie key: -timestamp, name)", o1)


def test_merge_order_prefers_contained_branch_first():
    g, branches, tags = _forked_repo()
    branches = {**branches, "behind": B("behind", "1")}
    planner = MergePlanner(g, branches, tags)
    steps = planner.plan_merge_order("main", ["topic-a", "behind", "topic-b"])
    log_case("ff-first", sources=["topic-a", "behind", "topic-b"])
    assert steps[0].source_branch == "behind"
    assert steps[0].action == "fast-forward"
    assert [s.action for s in steps[1:]] == ["merge", "merge"]
    LOG.info("DECISION: %s", [(s.source_branch, s.action) for s in steps])


def test_merge_order_divergence_depth_orders_older_fork_first():
    # main: 0 - 1 - 2 ; fork "recent" from 1, fork "ancient" from 0
    commits = [
        C("0", (), "", 0), C("1", ("0",), "", 1), C("2", ("1",), "", 2),
        C("r", ("1",), "", 3), C("a", ("0",), "", 3),
    ]
    g = CommitGraph().import_commits(commits); g.validate()
    branches = {"main": B("main", "2"), "recent": B("recent", "r"), "ancient": B("ancient", "a")}
    planner = MergePlanner(g, branches, {})
    steps = planner.plan_merge_order("main", ["recent", "ancient"])
    log_case("depth", merge_bases=[s.merge_base for s in steps])
    assert [s.source_branch for s in steps] == ["ancient", "recent"]
    LOG.info("DECISION: older divergence (base %s) merged before %s",
             steps[0].merge_base, steps[1].merge_base)


def test_release_bump_major_for_breaking_change():
    g, branches, tags = _forked_repo()
    g.add_commit(C("5", ("2",), "feat!: redesign api\n\nBREAKING CHANGE: drop v1", 5))
    branches = {**branches, "main": B("main", "5")}
    planner = MergePlanner(g, branches, tags)
    plan = planner.plan_release("main")
    log_case("major", head="5", base_tag="v1.0.0")
    log_reasons(plan)
    assert plan.bump == "major"
    assert plan.next_version == "2.0.0"


def test_release_minor_for_feat_and_changelog_order():
    g, branches, tags = _forked_repo()
    planner = MergePlanner(g, branches, tags)
    plan = planner.plan_release("main")
    log_case("minor", head="2", base_tag="v1.0.0")
    log_reasons(plan)
    assert plan.bump == "minor"
    assert plan.next_version == "1.1.0"
    assert [e.commit_id for e in plan.changelog] == ["2", "1"]
    LOG.info("DECISION: changelog=%s", [e.commit_id for e in plan.changelog])


def test_prerelease_suffix_and_tag_priority():
    g, branches, tags = _forked_repo()
    tags = {
        "v1.0.0": T("v1.0.0", "0"),
        "v1.1.0-rc.1": T("v1.1.0-rc.1", "1"),
    }
    planner = MergePlanner(g, branches, tags)
    # stable release computation ignores prerelease tags for the base
    plan = planner.plan_release("main")
    log_case("prerelease-base", tags=list(tags), prerelease=None)
    log_reasons(plan)
    assert plan.base_version == "1.0.0"
    assert plan.next_version == "1.1.0"
    # opening a prerelease line from the stable base -> rc.1
    g2, b2, _ = _forked_repo()
    p2 = MergePlanner(g2, b2, {"v1.0.0": T("v1.0.0", "0")})
    pre = p2.plan_release("main", prerelease="rc")
    log_reasons(pre)
    assert pre.next_version == "1.1.0-rc.1"
    # continuing the existing rc line -> counter advances, core unchanged
    again = planner.plan_release("main", prerelease="rc")
    log_reasons(again)
    assert again.base_version == "1.1.0-rc.1"
    assert again.next_version == "1.1.0-rc.2"
    # switching identifier restarts the counter on the same core
    beta = planner.plan_release("main", prerelease="beta")
    assert beta.next_version == "1.1.0-beta.1"
    LOG.info(
        "DECISION: stable=%s open=%s continue=%s switch=%s",
        plan.next_version, pre.next_version, again.next_version, beta.next_version,
    )


def test_changelog_dedup_across_duplicate_import_and_merge_history():
    # 0 -- 1 ------ 3 merge ; commit 1 reachable via two sides of history? no:
    # build diamond where a commit would be double-walked naively
    commits = [
        C("0", (), "init", 0),
        C("1", ("0",), "fix: shared-line", 1),
        C("2", ("1",), "feat: side", 2),
        C("3", ("1", "2"), "Merge", 3),
    ]
    g = CommitGraph().import_commits(commits); g.validate()
    branches = {"main": B("main", "3")}
    tags = {"v1.0.0": T("v1.0.0", "0")}
    planner = MergePlanner(g, branches, tags)
    plan = planner.plan_release("main")
    ids = [e.commit_id for e in plan.changelog]
    log_case("dedup", walk=ids)
    assert len(ids) == len(set(ids))
    assert "3" not in ids  # merge commit excluded
    assert set(ids) == {"1", "2"}
    LOG.info("DECISION: each commit counted once; merge 3 excluded; ids=%s", ids)


def test_no_base_version_raises_distinguishable_error():
    g, branches, _ = _forked_repo()
    planner = MergePlanner(g, branches, {})
    log_case("no-base", tags=[])
    with pytest.raises(NoBaseVersionError) as ei:
        planner.plan_release("main")
    assert ei.value.code == "no_base_version"
    LOG.info("DECISION: rejected code=%s", ei.value.code)


def test_unknown_branch_and_target_in_sources_rejected():
    g, branches, tags = _forked_repo()
    planner = MergePlanner(g, branches, tags)
    with pytest.raises(InvalidRefError) as ei:
        planner.plan_merge_order("main", ["main"])
    assert ei.value.code == "invalid_ref"
    with pytest.raises(InvalidRefError) as ei:
        planner.plan_merge_order("ghost", [])
    assert ei.value.code == "invalid_ref"
