"""Application service orchestrating repository access and planning.

All planning methods accept an optional ``revision`` so conclusions can be
reproduced against any historical snapshot. Data import runs as one atomic
locked operation: invalid input aborts the whole batch and no partial
snapshot is published.
"""

from .models import Branch, Tag
from .planner import MergePlanner
from .repository import Repository, StateSnapshot


class PlannerService:
    def __init__(self, repository: Repository | None = None) -> None:
        self.repository = repository or Repository()

    def _snapshot(self, revision: int | None) -> StateSnapshot:
        if revision is None:
            return self.repository.current()
        return self.repository.snapshot_at(revision)

    def import_data(self, payload: dict) -> dict:
        """Atomically import commits, branches and tags from one payload.

        Expected shape (all sections optional; legacy payloads missing
        sections are accepted)::

            {"commits": [...], "branches": [...], "tags": [...]}
        """
        if not isinstance(payload, dict):
            from .errors import InvalidRefError

            raise InvalidRefError("import payload must be an object", details={"type": type(payload).__name__})
        commits = payload.get("commits", []) or []
        branches = payload.get("branches", []) or []
        tags = payload.get("tags", []) or []
        # atomic: commit import validates the graph; refs are applied together
        report = self.repository.import_commits(commits)
        refs = self.repository.import_refs(branches=branches, tags=tags)
        report.update(refs)
        return report

    def _planner(self, revision: int | None) -> tuple[MergePlanner, StateSnapshot]:
        snap = self._snapshot(revision)
        return MergePlanner(snap.graph, snap.branches, snap.tags), snap

    def merge_order(
        self,
        target: str,
        sources: list[str],
        strategy: str = "merge",
        revision: int | None = None,
    ) -> dict:
        planner, snap = self._planner(revision)
        steps = planner.plan_merge_order(target, sources, strategy=strategy)
        return {
            "revision": snap.revision,
            "target": target,
            "strategy": strategy,
            "steps": [s.to_dict() for s in steps],
        }

    def release_plan(
        self,
        branch: str,
        options: dict | None = None,
        revision: int | None = None,
    ) -> dict:
        options = options or {}
        planner, snap = self._planner(revision)
        plan = planner.plan_release(
            branch=branch,
            base_version=options.get("base_version"),
            default_bump=options.get("default_bump", "patch"),
            prerelease=options.get("prerelease"),
        )
        result = plan.to_dict()
        result["revision"] = snap.revision
        result["branch"] = branch
        return result

    def state(self, revision: int | None = None) -> dict:
        snap = self._snapshot(revision)
        return {
            "revision": snap.revision,
            "commits": sorted(snap.graph.commits.keys()),
            "branches": {name: b.head for name, b in snap.branches.items()},
            "tags": {name: t.target for name, t in snap.tags.items()},
        }
