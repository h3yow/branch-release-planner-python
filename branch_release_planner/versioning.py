"""Semantic version parsing, precedence and next-version computation.

Implements Semantic Versioning 2.0.0 precedence rules:

* ``MAJOR.MINOR.PATCH`` numeric core comparison;
* build metadata (``+build``) never affects precedence;
* a version *with* a prerelease has lower precedence than the same core
  without one (``1.0.0-alpha < 1.0.0``);
* prerelease identifiers are compared left to right: numeric identifiers
  compare as integers and always rank below alphanumeric ones; a longer
  identifier list wins when every preceding identifier is equal.

A leading ``v``/``V`` prefix (common in tag names like ``v1.2.3``) is
accepted on input but never produced on output.
"""

import re
from dataclasses import dataclass

from .errors import InvalidVersionError

_VERSION_RE = re.compile(
    r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z-.]+))?(?:\+([0-9A-Za-z-.]+))?$"
)
_NUMERIC_RE = re.compile(r"^\d+$")
# bump kinds in priority order
MAJOR = "major"
MINOR = "minor"
PATCH = "patch"
NONE = "none"


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[object, ...] = ()
    build: str = ""

    @classmethod
    def parse(cls, text: str) -> "SemVer":
        return parse_version(text)

    def core(self) -> "SemVer":
        return SemVer(self.major, self.minor, self.patch)

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += "-" + ".".join(str(p) for p in self.prerelease)
        if self.build:
            s += "+" + self.build
        return s


def parse_version(text: str) -> SemVer:
    if not isinstance(text, str):
        raise InvalidVersionError("version must be a string", details={"value": repr(text)})
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise InvalidVersionError("not a valid semantic version", details={"value": text})
    major, minor, patch, pre, build = m.groups()
    prerelease: tuple[object, ...] = ()
    if pre is not None:
        ids: list[object] = []
        for ident in pre.split("."):
            if ident == "":
                raise InvalidVersionError("empty prerelease identifier", details={"value": text})
            if _NUMERIC_RE.match(ident):
                # leading zeros are forbidden for numeric identifiers
                if len(ident) > 1 and ident[0] == "0":
                    raise InvalidVersionError(
                        "numeric prerelease identifier must not have leading zero",
                        details={"value": text, "identifier": ident},
                    )
                ids.append(int(ident))
            else:
                ids.append(ident)
        prerelease = tuple(ids)
    return SemVer(int(major), int(minor), int(patch), prerelease, build or "")


def is_prerelease(version: SemVer) -> bool:
    return bool(version.prerelease)


def compare_versions(a: SemVer, b: SemVer) -> int:
    """Return -1/0/1 according to SemVer 2.0 precedence (build ignored)."""
    for x, y in ((a.major, b.major), (a.minor, b.minor), (a.patch, b.patch)):
        if x != y:
            return -1 if x < y else 1
    if a.prerelease == b.prerelease:
        return 0
    # no prerelease ranks higher than any prerelease
    if not a.prerelease:
        return 1
    if not b.prerelease:
        return -1
    for x, y in zip(a.prerelease, b.prerelease):
        if x == y:
            continue
        x_num = isinstance(x, int)
        y_num = isinstance(y, int)
        if x_num and y_num:
            return -1 if x < y else 1
        if x_num != y_num:
            return -1 if x_num else 1
        return -1 if str(x) < str(y) else 1
    # all shared identifiers equal -> longer list has higher precedence
    if len(a.prerelease) != len(b.prerelease):
        return -1 if len(a.prerelease) < len(b.prerelease) else 1
    return 0


def max_version(versions) -> SemVer | None:
    result = None
    for v in versions:
        if result is None or compare_versions(v, result) > 0:
            result = v
    return result


def bump(version: SemVer, kind: str, prerelease: str | None = None) -> SemVer:
    """Bump the release core. Prerelease/build metadata of the base is dropped."""
    core = version.core()
    if kind == MAJOR:
        nxt = SemVer(core.major + 1, 0, 0)
    elif kind == MINOR:
        nxt = SemVer(core.major, core.minor + 1, 0)
    elif kind == PATCH:
        nxt = SemVer(core.major, core.minor, core.patch + 1)
    elif kind == NONE:
        nxt = core
    else:
        raise InvalidVersionError("unknown bump kind", details={"kind": kind})
    if prerelease:
        return SemVer(nxt.major, nxt.minor, nxt.patch, (prerelease, 1))
    return nxt


def next_prerelease(version: SemVer, kind: str, identifier: str) -> SemVer:
    """Build the first prerelease of the next ``kind`` core, e.g. 1.3.0-rc.1."""
    return bump(version, kind, prerelease=identifier)


def advance_prerelease(version: SemVer, identifier: str) -> SemVer:
    """Move within a prerelease core, preserving ``MAJOR.MINOR.PATCH``.

    * ``1.1.0-rc.1`` + ``rc`` -> ``1.1.0-rc.2`` (same identifier: increment);
    * ``1.1.0-beta.2`` + ``rc`` -> ``1.1.0-rc.1`` (identifier changed:
      counter restarts but the targeted core stays 1.1.0);
    * ``1.1.0`` (stable) + ``rc`` -> ``1.1.0-rc.1``.

    Build metadata of the base is always dropped.
    """
    pre = version.prerelease
    if pre:
        if pre[0] == identifier and len(pre) >= 2 and isinstance(pre[-1], int):
            return SemVer(version.major, version.minor, version.patch, pre[:-1] + (pre[-1] + 1,))
        return SemVer(version.major, version.minor, version.patch, (identifier, 1))
    return SemVer(version.major, version.minor, version.patch, (identifier, 1))
