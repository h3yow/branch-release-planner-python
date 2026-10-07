"""HTTP 接口：整批导入、发布规划（合并/变基、多发布线）、修订号增量、快照回溯。"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from release_planner import (
    IntegrationMode,
    MultiLinePlanner,
    PlanError,
)
from release_planner.models import Commit, Tag
from release_planner.planner import ReleasePlanner
from release_planner.store import RepoStore

app = FastAPI(title="branch-release-planner")
store = RepoStore()


class CommitIn(BaseModel):
    id: str
    parents: list[str] = []
    message: str = ""
    branch: str = ""
    change: str = "patch"


class TagIn(BaseModel):
    name: str
    commit_id: str


class BranchIn(BaseModel):
    name: str
    head: str


class BatchIn(BaseModel):
    """整批原子导入：三类数据要么一起生效、要么一起不生效。"""

    commits: list[CommitIn] = []
    tags: list[TagIn] = []
    branches: dict[str, str] = {}


def _to_commit(c: CommitIn) -> Commit:
    return Commit(
        id=c.id, parents=tuple(c.parents), message=c.message,
        branch=c.branch, change=c.change,
    )


def _to_tag(t: TagIn) -> Tag:
    return Tag(name=t.name, commit_id=t.commit_id)


def _line_json(line) -> dict:
    return {
        "branch": line.branch,
        "head": line.head,
        "baseline": str(line.baseline) if line.baseline else None,
        "baseline_tag": line.baseline_tag,
        "next_version": str(line.next_version) if line.next_version else None,
        "changelog": line.changelog,
        "pending": line.pending,
        "used_tags": line.used_tags,
        "ignored_tags": [
            {"name": t.name, "commit_id": t.commit_id, "reason": t.reason}
            for t in line.ignored_tags
        ],
    }


@app.get("/")
def read_root():
    return {
        "service": "branch-release-planner",
        "snapshots": store.history_size(),
        "revision": store.current_revision(),
    }


# ---- 导入 ----

@app.post("/commits")
def import_commits(commits: list[CommitIn]):
    try:
        result = store.import_batch(commits=[_to_commit(c) for c in commits])
    except PlanError as e:
        raise HTTPException(status_code=409, detail={"reason": e.reason.value, "detail": e.detail})
    return {
        "added": result.added_commits,
        "revision": result.revision,
        "snapshots": store.history_size(),
    }


@app.post("/import")
def import_batch(batch: BatchIn):
    """整批原子导入提交 + 标签 + 分支头。"""
    try:
        result = store.import_batch(
            commits=[_to_commit(c) for c in batch.commits],
            tags=[_to_tag(t) for t in batch.tags],
            branches=batch.branches,
        )
    except PlanError as e:
        status = 404 if e.reason.value in ("unknown_branch", "unknown_tag") else 409
        raise HTTPException(status_code=status,
                            detail={"reason": e.reason.value, "detail": e.detail})
    return {
        "added_commits": result.added_commits,
        "added_tags": result.added_tags,
        "changed_branches": result.changed_branches,
        "revision": result.revision,
        "snapshots": store.history_size(),
    }


@app.post("/tags")
def add_tag(tag: TagIn):
    try:
        store.add_tag(_to_tag(tag))
    except PlanError as e:
        raise HTTPException(status_code=409, detail={"reason": e.reason.value, "detail": e.detail})
    return {"ok": True, "revision": store.current_revision()}


@app.post("/branches")
def set_branch(branch: BranchIn):
    try:
        store.set_branch(branch.name, branch.head)
    except PlanError as e:
        raise HTTPException(status_code=404, detail={"reason": e.reason.value, "detail": e.detail})
    return {"ok": True}


# ---- 规划 ----

@app.get("/plan")
def get_plan():
    try:
        planner = ReleasePlanner(store.graph(), store.tags())
        plan = planner.plan(store.branches())
    except PlanError as e:
        raise HTTPException(status_code=422, detail={"reason": e.reason.value, "detail": e.detail})
    return {
        "order": plan.order,
        "next_version": str(plan.next_version) if plan.next_version else None,
        "changelog": plan.changelog,
        "rationale": plan.rationale,
    }


def _multi_plan(mode: str):
    try:
        planner = MultiLinePlanner(store.graph(), store.tags())
        if mode == "rebase":
            return planner.plan_rebase(store.branches())
        return planner.plan_all(store.branches())
    except PlanError as e:
        status = 422 if e.reason.value == "rebase_unsupported" else 422
        raise HTTPException(status_code=status,
                            detail={"reason": e.reason.value, "detail": e.detail})


@app.get("/release-lines/plan")
def get_multi_plan():
    """多发布线合并式 + 变基式编排（变基不适用时返回 422/rebase_unsupported）。"""
    p = _multi_plan("all")
    return {
        "lines": [_line_json(line) for line in p.lines],
        "merge_order": p.merge_order,
        "rebase_order": p.rebase_order,
        "shared_commits": p.shared_commits,
        "rebase": {
            "order": p.rebase.order,
            "skipped": [
                {"commit_id": s.commit_id, "reason": s.reason, "branch": s.branch}
                for s in p.rebase.skipped
            ],
            "lines": {
                name: [{"action": s.action, "commit_id": s.commit_id, "reason": s.reason}
                       for s in steps]
                for name, steps in p.rebase.lines.items()
            },
            "rationale": p.rebase.rationale,
        },
        "rationale": p.rationale,
    }


@app.get("/release-lines/rebase")
def get_rebase_plan():
    """仅变基式顺序；不适合变基时返回 422/rebase_unsupported。"""
    rp = _multi_plan("rebase")
    return {
        "order": rp.order,
        "skipped": [
            {"commit_id": s.commit_id, "reason": s.reason, "branch": s.branch}
            for s in rp.skipped
        ],
        "rationale": rp.rationale,
    }


# ---- 修订号增量 ----

@app.get("/revision")
def get_revision():
    return {"revision": store.current_revision()}


@app.get("/commits-since/{revision}")
def commits_since(revision: int):
    try:
        result = store.commits_since(revision)
    except PlanError as e:
        raise HTTPException(status_code=404, detail={"reason": e.reason.value, "detail": e.detail})
    return {
        "since": result.since,
        "revision": result.revision,
        "commits": [
            {
                "id": c.id, "parents": list(c.parents), "message": c.message,
                "branch": c.branch, "change": c.change,
            }
            for c in result.commits
        ],
    }


# ---- 快照 ----

@app.post("/snapshots")
def make_snapshot():
    return {"version": store.snapshot()}


@app.post("/checkout/{version}")
def checkout(version: int):
    try:
        store.checkout(version)
    except PlanError as e:
        raise HTTPException(status_code=404, detail={"reason": e.reason.value, "detail": e.detail})
    return {"ok": True, "snapshots": store.history_size(), "revision": store.current_revision()}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
