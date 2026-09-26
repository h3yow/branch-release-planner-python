"""Concurrency, idempotent imports, snapshot replay and legacy field defaults."""

import logging
from concurrent.futures import ThreadPoolExecutor

from branch_release_planner.models import Branch, Commit, Tag
from branch_release_planner.repository import Repository
from branch_release_planner.service import PlannerService

from .conftest import B, C, T, log_case

LOG = logging.getLogger("planner-tests")


def test_concurrent_imports_lose_nothing_and_stay_valid():
    repo = Repository()
    # first establish the root so later batches don't violate missing parents
    root_rev = repo.import_commits([C("root", (), "init", 0)])["revision"]

    def batch(b: int):
        # each worker owns a disjoint chain rooted at "root"
        return repo.import_commits([
            C(f"w{b}-1", ("root",), f"w{b}-1", b * 10 + 1),
            C(f"w{b}-2", (f"w{b}-1",), f"w{b}-2", b * 10 + 2),
        ])

    log_case("concurrent-import", workers=8, per_worker=2)
    with ThreadPoolExecutor(max_workers=8) as ex:
        reports = list(ex.map(batch, range(8)))
    snap = repo.current()
    snap.graph.validate()  # published state must always be valid
    expected = {"root"} | {f"w{b}-{i}" for b in range(8) for i in (1, 2)}
    assert set(snap.graph.commits) == expected
    assert sum(r["commits_added"] for r in reports) == 16
    # revisions: 1 root + 8 worker batches
    assert repo.latest_revision() == root_rev + 8
    LOG.info("DECISION: %s commits present, no loss; revisions advanced to %s",
             len(snap.graph.commits), repo.latest_revision())


def test_repeated_import_is_idempotent_and_never_double_counted():
    repo = Repository()
    first = repo.import_commits([C("a", ()), C("b", ("a",), "fix: x", 1)])
    log_case("retry-import", first=first)
    second = repo.import_commits([C("a", ()), C("b", ("a",), "fix: x", 1)])
    assert first["commits_added"] == 2
    assert second["commits_added"] == 0
    assert second["commits_duplicated"] == 2
    assert len(repo.current().graph.commits) == 2
    # release changelog still lists b exactly once after re-import
    repo.upsert_tag(T("v1.0.0", "a"))
    repo.upsert_branch(B("main", "b"))
    svc = PlannerService(repo)
    plan = svc.release_plan("main")
    assert [e["commit_id"] for e in plan["changelog"]] == ["b"]
    assert plan["next_version"] == "1.0.1"
    LOG.info("DECISION: duplicate batch added=0 duplicated=2; changelog=['b']; version=%s",
             plan["next_version"])


def test_invalid_batch_is_atomic_no_partial_snapshot():
    repo = Repository()
    repo.import_commits([C("root", ())])
    before = repo.latest_revision()
    log_case("atomic-failure", batch=[("ok", ("root",)), ("bad", ("ghost",))])
    try:
        repo.import_commits([C("ok", ("root",)), C("bad", ("ghost",))])
        raise AssertionError("expected MissingParentError")
    except Exception as e:
        assert e.code == "missing_parent_commit"
    assert repo.latest_revision() == before  # nothing published
    assert "ok" not in repo.current().graph.commits
    LOG.info("DECISION: revision stayed at %s and partial commit 'ok' not visible", before)


def test_snapshot_replay_reproduces_conclusions():
    repo = Repository()
    repo.import_commits([C("0", (), "init", 0)])
    repo.upsert_tag(T("v1.0.0", "0"))
    repo.upsert_branch(B("main", "0"))
    repo.import_commits([C("1", ("0",), "fix: a", 1)])
    rev_at_fix = repo.upsert_branch(B("main", "1"))
    repo.import_commits([C("2", ("1",), "feat: b", 2)])
    rev_after_feat = repo.upsert_branch(B("main", "2"))
    svc = PlannerService(repo)
    log_case("snapshot", fix_rev=rev_at_fix, feat_rev=rev_after_feat)
    at_fix = svc.release_plan("main", revision=rev_at_fix)
    at_feat = svc.release_plan("main", revision=rev_after_feat)
    latest = svc.release_plan("main")
    assert at_fix["next_version"] == "1.0.1"
    assert at_feat["next_version"] == "1.1.0"
    # re-reading the old revision must reproduce the identical conclusion
    assert svc.release_plan("main", revision=rev_at_fix)["next_version"] == "1.0.1"
    assert latest["revision"] == repo.latest_revision()
    LOG.info("DECISION: rev%s->%s, rev%s->%s (reproducible)",
             rev_at_fix, at_fix["next_version"], rev_after_feat, at_feat["next_version"])


def test_legacy_payload_missing_fields_get_stable_defaults():
    log_case("legacy-fields", payload=[{"id": "z", "parents": ["y"]}, {"sha": "y"}])
    commit = Commit.from_dict({"id": "z", "parents": ["y"]})
    legacy = Commit.from_dict({"sha": "y"})  # legacy id alias
    branch = Branch.from_dict({"name": "main", "target": "y"})  # legacy head alias
    assert commit.message == "" and commit.timestamp == 0
    assert commit.parent_ids == ("y",)
    assert legacy.id == "y"
    assert branch.head == "y"
    svc = PlannerService(Repository())
    report = svc.import_data({"commits": [{"sha": "y"}, {"id": "z", "parents": ["y"]}]})
    assert report["commits_added"] == 2
    # empty payload / missing sections are accepted
    assert svc.import_data({})["commits_added"] == 0
    LOG.info("DECISION: legacy aliases parsed; defaults message='', timestamp=0; report=%s", report)
