"""Distinguishable failure reasons for invalid inputs and operations."""


class PlannerError(Exception):
    """Base error carrying a stable machine-readable ``code``."""

    code = "planner_error"

    def __init__(self, message: str = "", *, details: dict | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class CommitNotFoundError(PlannerError):
    code = "commit_not_found"


class DuplicateCommitError(PlannerError):
    code = "duplicate_commit"


class CyclicGraphError(PlannerError):
    code = "cyclic_parent_edge"


class MissingParentError(PlannerError):
    code = "missing_parent_commit"


class DuplicateCommitIdError(PlannerError):
    code = "duplicate_commit_id"


class InvalidVersionError(PlannerError):
    code = "invalid_version"


class InvalidRefError(PlannerError):
    code = "invalid_ref"


class EmptyGraphError(PlannerError):
    code = "empty_graph"


class NoBaseVersionError(PlannerError):
    code = "no_base_version"


class SnapshotNotFoundError(PlannerError):
    code = "snapshot_not_found"
