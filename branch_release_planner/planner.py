"""Merge/rebase ordering and release planning on a validated graph.

Determinism rules (results are independent of input ordering):

* merge sources are sorted by a fixed key
  ``(merge_base_depth_desc, head_timestamp_desc, name_asc)``:
  branches whose divergence point is older are merged first, because merging
  them first maximizes fast-forward opportunities and minimizes conflict
  surface; a branch already contained in the target (fast-forward only)
  always comes first;
* changelog commits are de-duplicated by commit id and ordered by a fixed
  ``(timestamp_desc, id_asc)`` key; merge commits never appear in the log;
* the base release is the highest-precedence semver tag whose target commit
  is an ancestor of the branch head; stable tags outrank prerelease tags
  unless prerelease tags are explicitly included;
* bump level follows Conventional Commits (BREAKING CHANGE > feat > fix),
  defaulting to patch when no recognizable message is present.
"""

import re

from .errors import (
    CommitNotFoundError,
    InvalidRefError,
    InvalidVersionError,
    NoBaseVersionError,
)
from .graph import CommitGraph
from .models import Branch, ChangelogEntry, MergePlanStep, ReleasePlan, Tag
from .versioning import (
    MAJOR,
    MINOR,
    PATCH,
    NONE,
    SemVer,
    advance_prerelease,
    bump,
    compare_versions,
    max_version,
    parse_version,
)

_BUMP_RANK = {NONE: 0, PATCH: 1, MINOR: 2, MAJOR: 3}
_FEAT_RE = re.compile(r"^feat(\(.+\))?!?:", re.IGNORECASE)
_FIX_RE = re.compile(r"^fix(\(.+\))?!?:", re.IGNORECASE)
_OTHER_CONV_RE = re.compile(r"^(build|chore|ci|docs|style|refactor|perf|test)(\(.+\))?!?:", re.IGNORECASE)
_BREAKING_RE = re.compile(r"BREAKING[ -]CHANGE", re.IGNORECASE)


def _tag_version(name: str) -> SemVer | None:
    text = name[1:] if name[:1] in ("v", "V") else name
    try:
        return parse_version(text)
    except InvalidVersionError:
        # tolerate a bare leading v: parse_version already handles it;
        # strip refs/tags/ prefix used by some legacy exports
        if name.startswith("refs/tags/"):
            return _tag_version(name[len("refs/tags/") :])
        return None


class MergePlanner:
    def __init__(
        self,
        graph: CommitGraph,
        branches: dict[str, Branch],
        tags: dict[str, Tag] | None = None,
    ):
        self.graph = graph
        self.branches = dict(branches)
        self.tags = dict(tags or {})

    # ------------------------------------------------------------- merge order
    def _branch(self, name: str) -> Branch:
        branch = self.branches.get(name)
        if branch is None:
            raise InvalidRefError("unknown branch", details={"branch": name})
        self.graph.get(branch.head)
        return branch

    def plan_merge_order(
        self, target: str, sources, strategy: str = "merge"
    ) -> list[MergePlanStep]:
        if strategy not in ("merge", "rebase"):
            raise InvalidRefError("strategy must be 'merge' or 'rebase'", details={"strategy": strategy})
        target_branch = self._branch(target)
        source_names = list(dict.fromkeys(sources))  # de-dup, keep first occurrence
        if target in source_names:
            raise InvalidRefError("target branch cannot also be a source", details={"branch": target})

        scored: list[tuple] = []
        for name in source_names:
            branch = self._branch(name)
            head = branch.head
            target_head = target_branch.head
            contained = self.graph.is_ancestor(head, target_head)
            base = target_head if contained else self.graph.merge_base(target_head, head)
            # depth = number of commits from target head back to the merge base:
            # larger depth => older divergence relative to target => earlier.
            depth = 0
            if base is not None:
                chain = self.graph.first_parent_chain(target_head)
                if base in chain:
                    depth = chain.index(base)
            ts = self.graph.get(head).timestamp
            scored.append((not contained, -depth, -ts, name, head, base, contained))

        # ff-only (contained) first, then greatest divergence depth, then
        # newest source head, then branch name ascending — fully deterministic.
        scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]))

        steps: list[MergePlanStep] = []
        for i, (_, _, _, name, head, base, contained) in enumerate(scored, start=1):
            if strategy == "rebase":
                action = "rebase"
            else:
                action = "fast-forward" if contained else "merge"
            steps.append(
                MergePlanStep(
                    order=i,
                    source_branch=name,
                    merge_base=base if base is not None else "",
                    action=action,
                )
            )
        return steps

    # ------------------------------------------------------------- release
    def _base_tag(
        self, head: str, include_prerelease: bool
    ) -> tuple[Tag | None, SemVer | None, list[str]]:
        reasons: list[str] = []
        candidates: list[tuple[SemVer, Tag]] = []
        for tag in self.tags.values():
            version = _tag_version(tag.name)
            if version is None:
                reasons.append(f"tag '{tag.name}' ignored: not a semantic version")
                continue
            if not self.graph.is_ancestor(tag.target, head):
                reasons.append(
                    f"tag '{tag.name}' ignored: target {tag.target} is not an ancestor of head {head}"
                )
                continue
            if version.prerelease and not include_prerelease:
                reasons.append(f"tag '{tag.name}' ignored: prerelease tags excluded by default")
                continue
            candidates.append((version, tag))
        if not candidates:
            return None, None, reasons
        # Stable releases always base on the highest stable tag (prereleases
        # excluded); prerelease builds base on the overall highest-precedence
        # reachable tag, which may itself be an in-flight prerelease whose
        # counter should then be advanced rather than restarted.
        if include_prerelease:
            pool = candidates
            why = "highest-precedence reachable tag (prerelease tags included)"
        else:
            pool = [(v, t) for v, t in candidates if not v.prerelease]
            if not pool:
                return None, None, reasons
            why = "highest-precedence reachable stable tag"
        version = max_version(v for v, _ in pool)
        tag = next(t for v, t in pool if compare_versions(v, version) == 0)
        reasons.append(f"base tag '{tag.name}' ({version}) selected as {why}")
        return tag, version, reasons

    def _classify(self, message: str) -> tuple[str, str]:
        """Return (bump_kind, reason) for one commit message."""
        if _BREAKING_RE.search(message or "") or (
            (_FEAT_RE.match(message or "") or _FIX_RE.match(message or "") or _OTHER_CONV_RE.match(message or ""))
            and message.split(":", 1)[0].rstrip().endswith("!")
        ):
            return MAJOR, "contains BREAKING CHANGE / '!' marker -> major"
        if _FEAT_RE.match(message or ""):
            return MINOR, "conventional 'feat' -> minor"
        if _FIX_RE.match(message or ""):
            return PATCH, "conventional 'fix' -> patch"
        if _OTHER_CONV_RE.match(message or ""):
            return NONE, "conventional non-feat/fix -> no forced bump"
        return NONE, "non-conventional message -> no forced bump"

    def plan_release(
        self,
        branch: str,
        base_version: SemVer | str | None = None,
        default_bump: str = PATCH,
        prerelease: str | None = None,
    ) -> ReleasePlan:
        br = self._branch(branch)
        head = br.head
        reasons: list[str] = []

        if base_version is not None:
            base = parse_version(str(base_version))
            base_commit: str | None = None
            reasons.append(f"base version {base} supplied explicitly; full head history considered")
        else:
            tag, base, tag_reasons = self._base_tag(head, include_prerelease=prerelease is not None)
            reasons.extend(tag_reasons)
            if base is None:
                raise NoBaseVersionError(
                    "no semantic-version tag reachable from branch head; supply base_version",
                    details={"branch": branch, "head": head},
                )
            base_commit = tag.target
            reasons.append(f"changelog starts after base tag commit {base_commit}")

        # collect commits strictly after the base point
        if base_commit is not None and self.graph.is_ancestor(base_commit, head):
            excluded = {base_commit} | self.graph.ancestors(base_commit)
            reachable = {head} | self.graph.ancestors(head)
            commit_ids = reachable - excluded
        else:
            commit_ids = {head} | self.graph.ancestors(head)

        entries: list[ChangelogEntry] = []
        seen: set[str] = set()
        best = NONE
        for cid in sorted(commit_ids, key=lambda c: (-self.graph.get(c).timestamp, c)):
            commit = self.graph.get(cid)
            if cid in seen:  # defensive: set already guarantees uniqueness
                continue
            seen.add(cid)
            if len(commit.parent_ids) >= 2:
                reasons.append(f"commit {cid} skipped in changelog: merge commit")
                continue
            kind, why = self._classify(commit.message)
            if _BUMP_RANK[kind] > _BUMP_RANK[best]:
                best = kind
                reasons.append(f"commit {cid}: {why}")
            else:
                reasons.append(f"commit {cid}: {why} (not higher than current '{best}')")
            entries.append(ChangelogEntry(commit_id=cid, branch=branch, message=commit.message))

        kind = best if best != NONE else default_bump
        if kind not in _BUMP_RANK:
            raise InvalidVersionError("invalid default_bump", details={"default_bump": default_bump})
        if prerelease and base.prerelease:
            # The base is itself an in-flight prerelease: stay on the same
            # core. Same identifier advances the counter (rc.1 -> rc.2);
            # a different identifier restarts it (beta.2 -> rc.1).
            next_ver = advance_prerelease(base, prerelease)
            if base.prerelease[0] == prerelease:
                why = f"continuing prerelease line '{prerelease}': {base} -> {next_ver}"
            else:
                why = (
                    f"switching prerelease identifier on unchanged core: "
                    f"{base} -> {next_ver}"
                )
            reasons.append(why)
        elif prerelease:
            # stable base: open a new prerelease line at the bumped core
            next_ver = bump(base, kind, prerelease=prerelease)
            reasons.append(
                f"bump '{kind}' applied to {base} opening prerelease line "
                f"'{prerelease}' -> {next_ver}"
            )
        else:
            next_ver = bump(base, kind)
            reasons.append(f"bump '{kind}' applied to {base} -> {next_ver}")
        return ReleasePlan(
            next_version=str(next_ver),
            bump=kind,
            base_version=str(base),
            changelog=tuple(entries),
            reasons=tuple(reasons),
        )
