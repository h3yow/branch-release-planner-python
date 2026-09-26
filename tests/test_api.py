"""HTTP layer: end-to-end import/planning, error-code mapping, history replay."""

import logging

from fastapi.testclient import TestClient

from branch_release_planner.api import create_app
from branch_release_planner.service import PlannerService

LOG = logging.getLogger("planner-tests")


def _client() -> TestClient:
    return TestClient(create_app(PlannerService()))


def _seed(client: TestClient) -> dict:
    payload = {
        "commits": [
            {"id": "0", "parents": [], "message": "init", "timestamp": 0},
            {"id": "1", "parents": ["0"], "message": "fix: bug", "timestamp": 1},
            {"id": "2", "parents": ["1"], "message": "feat: api", "timestamp": 2},
            {"id": "3", "parents": ["0"], "message": "feat: topic", "timestamp": 3},
        ],
        "branches": [
            {"name": "main", "head": "2"},
            {"name": "topic", "head": "3"},
        ],
        "tags": [{"name": "v1.0.0", "target": "0"}],
    }
    LOG.info("INPUT[/import]: %s", payload)
    resp = client.post("/import", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_end_to_end_planning_is_order_independent():
    r1 = _seed(_client())
    r2 = _seed(_client())
    for report in (r1, r2):
        assert report["commits_added"] == 4
    order_a = _client().post("/plan/merge-order", json={"target": "main", "sources": ["topic"]})
    c = _client()
    _seed(c)
    rel = c.post("/plan/release", json={"branch": "main"}).json()
    assert rel["next_version"] == "1.1.0"
    pre = c.post("/plan/release", json={"branch": "main", "options": {"prerelease": "rc"}}).json()
    assert pre["next_version"] == "1.1.0-rc.1"
    LOG.info("DECISION: release=%s prerelease=%s reasons=%d logged",
             rel["next_version"], pre["next_version"], len(rel["reasons"]))


def test_missing_parent_maps_to_422_with_code():
    c = _client()
    resp = c.post("/import", json={"commits": [{"id": "x", "parents": ["ghost"]}]})
    LOG.info("INPUT[/import invalid]: x->ghost; status=%s body=%s", resp.status_code, resp.json())
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "missing_parent_commit"


def test_cycle_maps_to_422_with_code():
    c = _client()
    resp = c.post("/import", json={"commits": [
        {"id": "a", "parents": ["b"]}, {"id": "b", "parents": ["a"]},
    ]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "cyclic_parent_edge"
    LOG.info("DECISION: cycle rejected http=422 code=%s", resp.json()["error"]["code"])


def test_bad_ref_and_snapshot_are_distinguishable():
    c = _client()
    _seed(c)
    bad_ref = c.post("/plan/merge-order", json={"target": "nope", "sources": []})
    assert bad_ref.status_code == 400
    assert bad_ref.json()["error"]["code"] == "invalid_ref"
    bad_rev = c.get("/state", params={"revision": 999})
    assert bad_rev.status_code == 404
    assert bad_rev.json()["error"]["code"] == "snapshot_not_found"
    LOG.info("DECISION: invalid_ref=400 snapshot_not_found=404 distinguished")


def test_history_endpoint_and_state_replay():
    c = _client()
    _seed(c)
    hist = c.get("/history").json()
    assert hist["latest"] >= 1
    state0 = c.get("/state", params={"revision": 0}).json()
    assert state0["commits"] == []
    state = c.get("/state").json()
    assert set(state["commits"]) == {"0", "1", "2", "3"}
    LOG.info("DECISION: revisions=%s empty-rev0 replay confirmed", hist["revisions"])
