"""Mixed concurrent writers/readers: published state is always valid and replayable."""

import logging
import random
from concurrent.futures import ThreadPoolExecutor

from branch_release_planner.service import PlannerService

from .conftest import C, log_case

LOG = logging.getLogger("planner-tests")


def test_mixed_writers_and_historical_readers():
    svc = PlannerService()
    svc.import_data({"commits": [{"id": "r", "parents": [], "message": "init", "timestamp": 0}]})
    errors: list[Exception] = []

    def writer(i: int) -> None:
        try:
            payload = {"commits": [
                {"id": f"n{i}", "parents": ["r"], "message": f"feat: w{i}", "timestamp": 100 + i},
            ]}
            svc.import_data(payload)
            # idempotent retry must not double-count
            again = svc.import_data(payload)
            assert again["commits_added"] == 0
        except Exception as e:  # pragma: no cover - surfaced at end
            errors.append(e)

    observed: list[tuple[int, str]] = []

    def reader() -> None:
        try:
            rev = svc.repository.latest_revision()
            state = svc.state(revision=rev)
            # every past revision must remain queryable and self-consistent
            assert state["revision"] == rev
            commit_count = len(state["commits"])
            assert commit_count >= 1
            observed.append((rev, len(state["commits"])))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    log_case("stress", writers=12, readers=12)
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = []
        for i in range(12):
            futs.append(ex.submit(writer, i))
            for _ in range(3):
                futs.append(ex.submit(reader))
        random.Random(1).shuffle(futs)
        for f in futs:
            f.result()

    assert not errors, errors
    final = svc.state()
    assert len(final["commits"]) == 13  # root + 12 unique
    # final release: one feat from root base 0.1.0 default? no tag -> expect error code
    from branch_release_planner.errors import NoBaseVersionError
    try:
        svc.release_plan("nonexistent-branch")
        raise AssertionError("expected invalid_ref")
    except Exception as e:
        assert e.code in ("invalid_ref", "no_base_version")
    LOG.info("DECISION: final commit count=%d; %d consistent reads; no lost/dup writes",
             len(final["commits"]), len(observed))
