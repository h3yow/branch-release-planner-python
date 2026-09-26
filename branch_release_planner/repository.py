"""Thread-safe repository state with immutable snapshot history.

Concurrency semantics:

* every public mutating operation holds a re-entrant lock for its whole
  critical section, so concurrent imports can never lose or interleave one
  another's changes (atomic read-modify-write);
* every mutation appends a *new* immutable :class:`StateSnapshot` with a
  monotonically increasing revision; old snapshots are never altered, so
  historical conclusions are reproducible via ``snapshot_at(revision)``;
* importing the same commit/branch/tag twice is an idempotent no-op and the
  response reports ``added`` vs ``duplicated`` counts separately, so a
  retried import never double-counts anything.
"""

import threading
from dataclasses import dataclass

from .errors import SnapshotNotFoundError
from .graph import CommitGraph
from .models import Branch, Commit, Tag


@dataclass(frozen=True)
class StateSnapshot:
    revision: int
    graph: CommitGraph
    branches: dict[str, Branch]
    tags: dict[str, Tag]


def _copy_snapshot(revision: int, previous: StateSnapshot | None) -> StateSnapshot:
    if previous is None:
        return StateSnapshot(revision, CommitGraph(), {}, {})
    return StateSnapshot(
        revision,
        CommitGraph(dict(previous.graph.commits)),
        dict(previous.branches),
        dict(previous.tags),
    )


class Repository:
    """In-memory repository guarded by a re-entrant lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshots: list[StateSnapshot] = [_copy_snapshot(0, None)]

    # ---------------------------------------------------------------- internal
    def _append(self, snapshot: StateSnapshot) -> None:
        self._snapshots.append(snapshot)

    # ----------------------------------------------------------------- commits
    def import_commits(self, commits) -> dict:
        """Atomically add commits; identical re-imports count as duplicates."""
        with self._lock:
            current = self._snapshots[-1]
            nxt = _copy_snapshot(current.revision + 1, current)
            added = 0
            duplicated = 0
            rejected = 0
            for raw in commits:
                commit = raw if isinstance(raw, Commit) else Commit.from_dict(raw)
                try:
                    is_new = nxt.graph.add_commit(commit)
                except Exception:
                    rejected += 1
                    raise
                if is_new:
                    added += 1
                else:
                    duplicated += 1
            # validate the *resulting* graph before publishing the snapshot
            nxt.graph.validate()
            # An import that changed nothing still records a revision so the
            # operation remains observable, but snapshots remain consistent.
            self._append(nxt)
            return {
                "revision": nxt.revision,
                "commits_added": added,
                "commits_duplicated": duplicated,
                "commits_rejected": rejected,
            }

    # ------------------------------------------------------------- branches/tags
    def upsert_branch(self, branch: Branch) -> int:
        with self._lock:
            current = self._snapshots[-1]
            nxt = _copy_snapshot(current.revision + 1, current)
            nxt.graph.get(branch.head)  # head must exist
            nxt.branches[branch.name] = branch
            self._append(nxt)
            return nxt.revision

    def upsert_tag(self, tag: Tag) -> int:
        with self._lock:
            current = self._snapshots[-1]
            nxt = _copy_snapshot(current.revision + 1, current)
            nxt.graph.get(tag.target)  # target must exist
            nxt.tags[tag.name] = tag
            self._append(nxt)
            return nxt.revision

    def import_refs(
        self,
        branches=(),
        tags=(),
    ) -> dict:
        """Atomic combined import of branches and tags."""
        with self._lock:
            current = self._snapshots[-1]
            nxt = _copy_snapshot(current.revision + 1, current)
            b_added = 0
            t_added = 0
            for raw in branches:
                branch = raw if isinstance(raw, Branch) else Branch.from_dict(raw)
                nxt.graph.get(branch.head)
                if branch.name not in nxt.branches:
                    b_added += 1
                nxt.branches[branch.name] = branch
            for raw in tags:
                tag = raw if isinstance(raw, Tag) else Tag.from_dict(raw)
                nxt.graph.get(tag.target)
                if tag.name not in nxt.tags:
                    t_added += 1
                nxt.tags[tag.name] = tag
            self._append(nxt)
            return {
                "revision": nxt.revision,
                "branches_added": b_added,
                "tags_added": t_added,
            }

    # ------------------------------------------------------------------ reads
    def current(self) -> StateSnapshot:
        with self._lock:
            return self._snapshots[-1]

    def snapshot_at(self, revision: int) -> StateSnapshot:
        with self._lock:
            if revision < 0 or revision >= len(self._snapshots):
                raise SnapshotNotFoundError(
                    "revision does not exist",
                    details={
                        "revision": revision,
                        "latest": self._snapshots[-1].revision,
                    },
                )
            return self._snapshots[revision]

    def history(self) -> list[StateSnapshot]:
        with self._lock:
            return list(self._snapshots)

    def latest_revision(self) -> int:
        with self._lock:
            return self._snapshots[-1].revision
