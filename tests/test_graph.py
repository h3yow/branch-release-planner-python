"""Graph import/query: forks, forks/merges, cycles, missing parents, duplicates."""

import logging

import pytest

from branch_release_planner.errors import (
    CyclicGraphError,
    DuplicateCommitIdError,
    MissingParentError,
)
from branch_release_planner.graph import CommitGraph

from .conftest import C, log_case

LOG = logging.getLogger("planner-tests")


def _fork_graph() -> CommitGraph:
    # A -- B ------ E (merge of B,D)
    #  \-- C -- D --/
    return CommitGraph().import_commits([
        C("A", (), "init", 0),
        C("B", ("A",), "b", 1),
        C("C", ("A",), "c", 2),
        C("D", ("C",), "d", 3),
        C("E", ("B", "D"), "Merge branch 'd'", 4),
    ])


def test_fork_merge_detection_and_merge_base():
    g = _fork_graph()
    log_case("fork-merge", commits=sorted(g.commits))
    g.validate()
    assert g.is_merge_commit("E")
    assert not g.is_merge_commit("B")
    assert g.merge_base("B", "D") == "A"
    order = g.topological_sort(["E"])
    assert order[0] == "A" and order[-1] == "E"
    pos = {cid: i for i, cid in enumerate(order)}
    for cid in order:
        assert all(pos[p] < pos[cid] for p in g.get(cid).parent_ids)
    LOG.info("DECISION: merge_base(B,D)=A; E recognized as merge; topological order=%s", order)


def test_ancestor_queries():
    g = _fork_graph()
    assert g.is_ancestor("A", "E")
    assert g.is_ancestor("D", "E")
    assert not g.is_ancestor("B", "D")
    assert g.first_parent_chain("E") == ["E", "B", "A"]


def test_missing_parent_is_rejected_with_distinct_code():
    g = CommitGraph().import_commits([C("X", ("ghost",))])
    log_case("missing-parent", commits=["X"], parents=["ghost"])
    with pytest.raises(MissingParentError) as ei:
        g.validate()
    assert ei.value.code == "missing_parent_commit"
    assert ei.value.details["parent"] == "ghost"
    LOG.info("DECISION: rejected code=%s details=%s", ei.value.code, ei.value.details)


@pytest.mark.parametrize(
    "commits",
    [
        [C("1", ("1",))],
        [C("1", ("2",)), C("2", ("1",))],
        [C("1", ("2",)), C("2", ("3",)), C("3", ("1",))],
    ],
)
def test_cycles_rejected(commits):
    g = CommitGraph().import_commits(commits)
    log_case("cycle", commits=[c.id for c in g.commits.values()])
    with pytest.raises(CyclicGraphError) as ei:
        g.validate()
    assert ei.value.code == "cyclic_parent_edge"
    LOG.info("DECISION: rejected code=%s cycle=%s", ei.value.code, ei.value.details.get("cycle"))


def test_duplicate_identical_commit_is_idempotent_conflicting_is_error():
    g = CommitGraph().import_commits([C("A", (), "init", 0)])
    assert g.add_commit(C("A", (), "init", 0)) is False
    log_case("duplicate", commit="A", action="re-import identical then conflicting")
    with pytest.raises(DuplicateCommitIdError) as ei:
        g.add_commit(C("A", ("X",)))
    assert ei.value.code == "duplicate_commit_id"
    LOG.info("DECISION: identical repeat no-op; conflicting body rejected code=%s", ei.value.code)
