"""HTTP 接口：提交导入、发布规划、快照回溯。"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from release_planner import PlanError
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


@app.get("/")
def read_root():
    return {"service": "branch-release-planner", "snapshots": store.history_size()}


@app.post("/commits")
def import_commits(commits: list[CommitIn]):
    try:
        added = store.import_commits(
            [Commit(id=c.id, parents=tuple(c.parents), message=c.message,
                    branch=c.branch, change=c.change) for c in commits]
        )
    except PlanError as e:
        raise HTTPException(status_code=409, detail={"reason": e.reason.value, "detail": e.detail})
    return {"added": added, "snapshots": store.history_size()}


@app.post("/tags")
def add_tag(tag: TagIn):
    store.add_tag(Tag(name=tag.name, commit_id=tag.commit_id))
    return {"ok": True}


@app.post("/branches")
def set_branch(branch: BranchIn):
    try:
        store.set_branch(branch.name, branch.head)
    except PlanError as e:
        raise HTTPException(status_code=404, detail={"reason": e.reason.value, "detail": e.detail})
    return {"ok": True}


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


@app.post("/snapshots")
def make_snapshot():
    return {"version": store.snapshot()}


@app.post("/checkout/{version}")
def checkout(version: int):
    try:
        store.checkout(version)
    except PlanError as e:
        raise HTTPException(status_code=404, detail={"reason": e.reason.value, "detail": e.detail})
    return {"ok": True, "snapshots": store.history_size()}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
