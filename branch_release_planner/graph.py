"""Commit graph import, validation and reachability queries.

The graph is a directed graph of commits with edges child -> parent.
Validation distinguishes these failure reasons instead of silently
skipping bad input:

* :class:`DuplicateCommitIdError` - the same id imported twice with a
  *different* body (re-importing an identical commit is idempotent);
* :class:`MissingParentError` - a parent id is not present in the graph;
* :class:`CyclicGraphError` - parent edges form a directed cycle.
"""

from .errors import (
    CyclicGraphError,
    DuplicateCommitIdError,
    MissingParentError,
)
from .models import Commit


class CommitGraph:
    def __init__(self, commits: dict[str, Commit] | None = None) -> None:
        self.commits: dict[str, Commit] = dict(commits or {})

    # ------------------------------------------------------------------ import
    def add_commit(self, commit: Commit) -> bool:
        """Add one commit. Returns True if newly added, False if identical repeat.

        Re-importing the exact same commit is an idempotent no-op. The same id
        with a conflicting body is rejected (it cannot be both two commits).
        """
        existing = self.commits.get(commit.id)
        if existing is not None:
            if existing != commit:
                raise DuplicateCommitIdError(
                    "commit id already imported with a different body",
                    details={"commit": commit.id},
                )
            return False
        self.commits[commit.id] = commit
        return True

    def import_commits(self, commits) -> "CommitGraph":
        """Import an iterable of :class:`Commit`, returning self."""
        for commit in commits:
            self.add_commit(commit)
        return self

    # -------------------------------------------------------------- validation
    def validate(self) -> None:
        """Check every parent exists and no parent-edge cycle exists."""
        for cid, commit in self.commits.items():
            for pid in commit.parent_ids:
                if pid not in self.commits:
                    raise MissingParentError(
                        "parent commit is not present in the graph",
                        details={"commit": cid, "parent": pid},
                    )
        # iterative DFS 3-color cycle detection
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {cid: WHITE for cid in self.commits}
        for start in self.commits:
            if color[start] != WHITE:
                continue
            stack: list[tuple[str, int]] = [(start, 0)]
            color[start] = GRAY
            while stack:
                node, idx = stack[-1]
                parents = self.commits[node].parent_ids
                if idx < len(parents):
                    nxt = parents[idx]
                    stack[-1] = (node, idx + 1)
                    if color[nxt] == GRAY:
                        cycle = [n for n, _ in stack]
                        raise CyclicGraphError(
                            "parent edges form a cycle",
                            details={"commit": nxt, "cycle": cycle + [nxt]},
                        )
                    if color[nxt] == WHITE:
                        color[nxt] = GRAY
                        stack.append((nxt, 0))
                else:
                    color[node] = BLACK
                    stack.pop()

    # ----------------------------------------------------------------- queries
    def get(self, commit_id: str) -> Commit:
        commit = self.commits.get(commit_id)
        if commit is None:
            from .errors import CommitNotFoundError

            raise CommitNotFoundError("unknown commit id", details={"commit": commit_id})
        return commit

    def ancestors(self, commit_id: str) -> set[str]:
        """All ancestors (parents transitively) of ``commit_id``; excludes itself."""
        self.get(commit_id)
        result: set[str] = set()
        stack = list(self.commits[commit_id].parent_ids)
        while stack:
            node = stack.pop()
            if node in result:
                continue
            result.add(node)
            stack.extend(self.commits[node].parent_ids)
        return result

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        if ancestor == descendant:
            return True
        return ancestor in self.ancestors(descendant)

    def is_merge_commit(self, commit_id: str) -> bool:
        return len(self.get(commit_id).parent_ids) >= 2

    def merge_base(self, a: str, b: str) -> str | None:
        """Best common ancestor of two commits (single best merge base)."""
        self.get(a)
        self.get(b)
        common = self.ancestors(a) & self.ancestors(b)
        # a commit itself counts as a merge base of the pair
        if a == b:
            return a
        if a in common or self.is_ancestor(a, b):
            return a
        if b in common or self.is_ancestor(b, a):
            return b
        if not common:
            return None
        # the best merge base is a common ancestor not having another common
        # ancestor as its descendant; pick deterministically by (timestamp, id)
        candidates = [
            c for c in common if not any(self.is_ancestor(d, c) and d != c for d in common)
        ]
        candidates.sort(key=lambda c: (self.commits[c].timestamp, c))
        return candidates[-1]

    def first_parent_chain(self, head: str) -> list[str]:
        """Head-first list following first parents (linearized trunk view)."""
        self.get(head)
        chain: list[str] = []
        node: str | None = head
        while node is not None:
            chain.append(node)
            parents = self.commits[node].parent_ids
            node = parents[0] if parents else None
        return chain

    def topological_sort(self, heads) -> list[str]:
        """Return reachable commits from ``heads`` in child-before-parent order.

        Ties are broken deterministically by (-timestamp, id) so results do
        not depend on dict insertion order.
        """
        visited: set[str] = set()
        order: list[str] = []

        def visit(node: str) -> None:
            if node in visited:
                return
            visited.add(node)
            parents = list(self.commits[node].parent_ids)
            parents.sort(key=lambda p: (-self.commits[p].timestamp, p))
            for p in parents:
                visit(p)
            order.append(node)

        for h in heads:
            visit(h)
        return order
