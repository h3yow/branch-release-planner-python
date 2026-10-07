"""数据模型：提交、分支、标签、语义化版本与规划结果。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import total_ordering

from .errors import FailureReason, PlanError

_SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


@dataclass(frozen=True)
class Commit:
    """一次提交。parents 按顺序记录父提交 id（合并提交有多个）。"""

    id: str
    parents: tuple[str, ...] = ()
    message: str = ""
    branch: str = ""
    # 变更类型用于版本提升判定：major/minor/patch/none
    change: str = "patch"


@dataclass(frozen=True)
class Tag:
    """一个语义化版本标签，指向某次提交。"""

    name: str            # 例如 "v1.2.3-rc.1+build.5"
    commit_id: str


def _split_identifiers(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(raw.split("."))


@total_ordering
@dataclass(frozen=True, order=False)
class SemVer:
    """语义化版本：major.minor.patch[-prerelease][+build]。

    排序规则（semver 2.0.0）：
    - 先比较 major/minor/patch；
    - 有预发布段的版本低于同核心号的正式版；
    - 预发布段逐段比较：纯数字段按数值、字母段按字典序，数字段低于字母段，
      公共前缀相同则段数多者更高；
    - 构建元数据（+build）不参与排序，但保留在对象中以保证可解释输出。
    """

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> "SemVer":
        m = _SEMVER_RE.match(text.strip())
        if not m:
            raise PlanError(FailureReason.INVALID_VERSION, f"无法解析版本号: {text!r}")
        return cls(
            major=int(m.group("major")),
            minor=int(m.group("minor")),
            patch=int(m.group("patch")),
            prerelease=_split_identifiers(m.group("prerelease")),
            build=_split_identifiers(m.group("build")),
        )

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += "-" + ".".join(self.prerelease)
        if self.build:
            s += "+" + ".".join(self.build)
        return s

    def _pre_key(self) -> tuple:
        # 预发布段比较键：数字段 -> (0, 数值, "")，字母段 -> (1, 0, 段)
        key: list[tuple] = []
        for ident in self.prerelease:
            if ident.isdigit():
                key.append((0, int(ident), ""))
            else:
                key.append((1, 0, ident))
        return tuple(key)

    def __lt__(self, other: "SemVer") -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        core_a = (self.major, self.minor, self.patch)
        core_b = (other.major, other.minor, other.patch)
        if core_a != core_b:
            return core_a < core_b
        # 正式版高于预发布版
        if bool(self.prerelease) != bool(other.prerelease):
            return bool(self.prerelease)
        return self._pre_key() < other._pre_key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return (
            (self.major, self.minor, self.patch, self.prerelease)
            == (other.major, other.minor, other.patch, other.prerelease)
        )

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def bump(self, level: str) -> "SemVer":
        """按变更级别提升版本；提升后清除预发布段与构建元数据。"""
        if level == "major":
            return SemVer(self.major + 1, 0, 0)
        if level == "minor":
            return SemVer(self.major, self.minor + 1, 0)
        if level == "patch":
            return SemVer(self.major, self.minor, self.patch + 1)
        return SemVer(self.major, self.minor, self.patch)


@dataclass
class MergePlan:
    """合并/变基执行顺序与版本结论。"""

    order: list[str] = field(default_factory=list)       # 提交 id 的执行顺序
    next_version: SemVer | None = None
    changelog: list[str] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)   # 判定依据，可解释输出
    skipped: list[str] = field(default_factory=list)     # 变基时被跳过的提交（如合并提交）


@dataclass
class LinePlan:
    """单条发布线的规划结果。"""

    name: str
    plan: MergePlan = field(default_factory=MergePlan)
    # 因不属于本线历史而被忽略的标签（附带忽略原因）
    ignored_tags: list[str] = field(default_factory=list)


@dataclass
class MultiLinePlan:
    """多条发布线的整体规划结果。"""

    lines: dict[str, LinePlan] = field(default_factory=dict)
    # 跨线共享已发布部分时，所有线待发布提交的统一执行顺序（确定、可复现）
    global_order: list[str] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)
