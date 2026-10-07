"""HTTP 接口：原子批量导入、导入修订号、多发布线规划、合并/变基顺序、快照回溯。"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from release_planner import PlanError
from release_planner.integration import rebase_plan
from release_planner.lines import plan_lines
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
    commits: list[CommitIn] = []
    tags: list[TagIn] = []
    branches: list[BranchIn] = []


class LinesIn(BaseModel):
    # 线名 -> 参与该线的分支名列表（分支头取当前仓库状态）
    lines: dict[str, list[str]]


def _err(status: int, e: PlanError) -> HTTPException:
    return HTTPException(status_code=status,
                         detail={"reason": e.reason.value, "detail": e.detail})


def _plan_dict(plan) -> dict:
    return {
        "order": plan.order,
        "next_version": str(plan.next_version) if plan.next_version else None,
        "changelog": plan.changelog,
        "rationale": plan.rationale,
        "skipped": plan.skipped,
    }


@app.get("/")
def read_root():
    return {"service": "branch-release-planner", "revision": store.revision()}


@app.post("/commits")
def import_commits(commits: list[CommitIn]):
    try:
        added = store.import_commits(
            [Commit(id=c.id, parents=tuple(c.parents), message=c.message,
                    branch=c.branch, change=c.change) for c in commits]
        )
    except PlanError as e:
        raise _err(409, e)
    return {"added": added, "revision": store.revision()}


@app.post("/batches")
def import_batch(batch: BatchIn):
    """原子批量导入：整批生效或整批不生效，返回生效后的导入修订号。"""
    try:
        rev = store.import_batch(
            commits=[Commit(id=c.id, parents=tuple(c.parents), message=c.message,
                            branch=c.branch, change=c.change) for c in batch.commits],
            tags=[Tag(name=t.name, commit_id=t.commit_id) for t in batch.tags],
            branches={b.name: b.head for b in batch.branches},
        )
    except PlanError as e:
        raise _err(409, e)
    return {"revision": rev}


@app.get("/revision")
def get_revision():
    return {"revision": store.revision()}


@app.get("/commits/since/{revision}")
def commits_since(revision: int):
    """从指定修订号到现在新增且尚未发布的提交（拓扑序，不重不漏）。"""
    try:
        commits = store.commits_since(revision)
    except PlanError as e:
        raise _err(404, e)
    return {"since": revision, "current": store.revision(), "commits": commits}


@app.post("/tags")
def add_tag(tag: TagIn):
    store.add_tag(Tag(name=tag.name, commit_id=tag.commit_id))
    return {"ok": True, "revision": store.revision()}


@app.post("/branches")
def set_branch(branch: BranchIn):
    try:
        store.set_branch(branch.name, branch.head)
    except PlanError as e:
        raise _err(404, e)
    return {"ok": True, "revision": store.revision()}


@app.get("/plan")
def get_plan():
    """合并式集成顺序（保留合并点，父先于子）。"""
    try:
        planner = ReleasePlanner(store.graph(), store.tags())
        plan = planner.plan(store.branches())
    except PlanError as e:
        raise _err(422, e)
    return _plan_dict(plan)


@app.get("/plan/rebase")
def get_rebase_plan():
    """变基式集成顺序（直线历史，合并提交被丢弃并列入 skipped）。"""
    try:
        plan = rebase_plan(store.graph(), store.tags(), store.branches())
    except PlanError as e:
        raise _err(422, e)
    return _plan_dict(plan)


@app.post("/plan/lines")
def get_lines_plan(body: LinesIn):
    """多发布线规划：每条线独立算版本与变更日志，并给出跨线统一执行顺序。"""
    branches = store.branches()
    try:
        lines: dict[str, dict[str, str]] = {}
        for line, names in body.lines.items():
            lines[line] = {}
            for n in names:
                if n not in branches:
                    from release_planner import FailureReason
                    raise PlanError(FailureReason.UNKNOWN_BRANCH, f"分支 {n!r} 不存在")
                lines[line][n] = branches[n]
        result = plan_lines(store.graph(), store.tags(), lines)
    except PlanError as e:
        raise _err(422, e)
    return {
        "lines": {
            name: {**_plan_dict(lp.plan), "ignored_tags": lp.ignored_tags}
            for name, lp in result.lines.items()
        },
        "global_order": result.global_order,
        "rationale": result.rationale,
    }


@app.post("/snapshots")
def make_snapshot():
    return {"version": store.snapshot()}


@app.post("/checkout/{version}")
def checkout(version: int):
    try:
        store.checkout(version)
    except PlanError as e:
        raise _err(404, e)
    return {"ok": True, "revision": store.revision()}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
