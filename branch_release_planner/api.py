"""FastAPI HTTP layer exposing planning and repository operations.

Failure responses use HTTP 4xx/5xx with the distinguishable ``code`` from
:mod:`branch_release_planner.errors` in the JSON body, so callers can branch
on the exact failure reason.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import PlannerError
from .service import PlannerService

_STATUS_FOR_CODE = {
    "commit_not_found": 404,
    "snapshot_not_found": 404,
    "duplicate_commit": 409,
    "duplicate_commit_id": 409,
    "cyclic_parent_edge": 422,
    "missing_parent_commit": 422,
    "invalid_version": 422,
    "invalid_ref": 400,
    "empty_graph": 422,
    "no_base_version": 409,
}


def create_app(service: PlannerService | None = None) -> FastAPI:
    app = FastAPI(
        title="Branch Release Planner",
        version="0.1.0",
        description="Local branch merge orchestration and semantic release planning.",
    )
    app.state.service = service or PlannerService()

    @app.exception_handler(PlannerError)
    async def _handle_planner_error(_: Request, exc: PlannerError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS_FOR_CODE.get(exc.code, 400),
            content={"error": exc.to_dict()},
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "revision": app.state.service.repository.latest_revision()}

    @app.get("/state")
    def get_state(revision: int | None = None) -> dict:
        return app.state.service.state(revision)

    @app.post("/import")
    def import_data(payload: dict) -> dict:
        return app.state.service.import_data(payload)

    @app.post("/plan/merge-order")
    def merge_order(payload: dict) -> dict:
        target = payload.get("target")
        sources = payload.get("sources") or []
        strategy = payload.get("strategy", "merge")
        revision = payload.get("revision")
        if not target or not isinstance(sources, list):
            from .errors import InvalidRefError

            raise InvalidRefError(
                "payload requires 'target' (str) and 'sources' (list[str])",
                details={"payload": payload},
            )
        return app.state.service.merge_order(target, sources, strategy, revision)

    @app.post("/plan/release")
    def release_plan(payload: dict) -> dict:
        branch = payload.get("branch")
        if not branch:
            from .errors import InvalidRefError

            raise InvalidRefError("payload requires 'branch'", details={"payload": payload})
        options = payload.get("options") or {}
        revision = payload.get("revision")
        return app.state.service.release_plan(branch, options, revision)

    @app.get("/history")
    def history() -> dict:
        snaps = app.state.service.repository.history()
        return {"revisions": [s.revision for s in snaps], "latest": snaps[-1].revision}

    return app
