"""Core data models describing commits, branches, tags and releases.

Parsers tolerate legacy payloads where optional fields are absent:
missing fields fall back to stable empty/zero defaults rather than failing.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Commit:
    id: str
    parent_ids: tuple[str, ...] = field(default_factory=tuple)
    message: str = ""
    author: str = ""
    timestamp: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "Commit":
        if not isinstance(data, dict):
            raise TypeError("commit payload must be an object")
        commit_id = data.get("id") or data.get("sha") or data.get("hash")
        if not commit_id or not isinstance(commit_id, str):
            from .errors import InvalidRefError

            raise InvalidRefError("commit requires a non-empty string 'id'", details={"payload": data})
        parents = data.get("parent_ids", data.get("parents", ()))
        if parents is None:
            parents = ()
        if not isinstance(parents, (list, tuple)):
            from .errors import InvalidRefError

            raise InvalidRefError("commit 'parent_ids' must be a list", details={"commit": commit_id})
        parent_ids = tuple(str(p) for p in parents)
        # Legacy format used 'msg'; both spellings default to "".
        message = str(data.get("message", data.get("msg", "")) or "")
        author = str(data.get("author", "") or "")
        # Legacy timestamps may be missing -> 0 keeps ordering stable.
        timestamp = int(data.get("timestamp", 0) or 0)
        return cls(commit_id, parent_ids, message, author, timestamp)


@dataclass(frozen=True)
class Branch:
    name: str
    head: str

    @classmethod
    def from_dict(cls, data: dict) -> "Branch":
        if not isinstance(data, dict):
            raise TypeError("branch payload must be an object")
        name = data.get("name")
        head = data.get("head") or data.get("target")
        if not name or not isinstance(name, str):
            from .errors import InvalidRefError

            raise InvalidRefError("branch requires a non-empty string 'name'", details={"payload": data})
        if not head or not isinstance(head, str):
            from .errors import InvalidRefError

            raise InvalidRefError("branch requires a non-empty string 'head'", details={"branch": name})
        return cls(name, head)


@dataclass(frozen=True)
class Tag:
    name: str
    target: str

    @classmethod
    def from_dict(cls, data: dict) -> "Tag":
        if not isinstance(data, dict):
            raise TypeError("tag payload must be an object")
        name = data.get("name")
        target = data.get("target") or data.get("head")
        if not name or not isinstance(name, str):
            from .errors import InvalidRefError

            raise InvalidRefError("tag requires a non-empty string 'name'", details={"payload": data})
        if not target or not isinstance(target, str):
            from .errors import InvalidRefError

            raise InvalidRefError("tag requires a non-empty string 'target'", details={"tag": name})
        return cls(name, str(target))


@dataclass(frozen=True)
class ChangelogEntry:
    commit_id: str
    branch: str
    message: str


@dataclass(frozen=True)
class MergePlanStep:
    order: int
    source_branch: str
    merge_base: str
    action: str

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "source_branch": self.source_branch,
            "merge_base": self.merge_base,
            "action": self.action,
        }


@dataclass(frozen=True)
class ReleasePlan:
    next_version: str
    bump: str
    base_version: str
    changelog: tuple[ChangelogEntry, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "next_version": self.next_version,
            "bump": self.bump,
            "base_version": self.base_version,
            "changelog": [
                {"commit_id": e.commit_id, "branch": e.branch, "message": e.message}
                for e in self.changelog
            ],
            "reasons": list(self.reasons),
        }
