"""仓库状态存储：整批原子导入、导入修订号、并发读写与历史快照回溯。

整批语义：
- 一次导入（可同时含提交、标签、分支头）要么整批生效，要么整批不生效；
  批次内任何冲突或引用缺失都会在改动发生前检出（pre-validate under lock），
  失败后图、标签、分支以及对外可见的导入修订号都保持不变。
- 导入修订号（revision）从 0 开始，仅当批次真正引入了新提交时才 +1；
  同一批数据重复导入没有新增提交，修订号不增长。
- 修订号单调递增：并发批次在同一把互斥锁内依次提交，各自拿到互不相同、
  严格递增的修订号，提交内容与修订号一一对应，不会错位。
- ``commits_since(rev)`` 查询修订号 rev 之后引入、且截至当前仍未发布
  （不属于任何现存标签提交祖先）的提交，按 (修订号, 提交 id) 稳定排序，
  跨批次去重，同一提交不会出现两次。
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field

from .errors import FailureReason, PlanError
from .graph import CommitGraph
from .models import Commit, Tag


@dataclass
class BatchResult:
    """一次整批导入的结果。"""

    added_commits: int = 0
    added_tags: int = 0
    changed_branches: int = 0
    revision: int = 0
    snapshot: int = 0


@dataclass
class IncrementalResult:
    """从某修订号到当前的增量查询结果。"""

    since: int = 0
    revision: int = 0
    commits: list[Commit] = field(default_factory=list)


class RepoStore:
    """线程安全的仓库状态，支持整批原子导入、修订号增量查询与快照回溯。

    - 所有写操作在同一把可重入互斥锁内完成“先校验、后落盘”，并发导入不会
      丢失或重复应用变更；失败批次不留任何副作用。
    - 每次成功改变状态的写批次自动生成一个不可变深拷贝快照；快照同时保存
      当前修订号与“提交 -> 引入修订号”的账本，checkout 后修订号与状态一致。
    - import_records 兼容旧格式：字段缺失时使用稳定缺省值
      （parents=()、message=""、branch=""、change="patch"）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._graph = CommitGraph()
        self._tags: list[Tag] = []
        self._branches: dict[str, str] = {}
        self._revision = 0
        # commit_id -> 引入该提交的修订号
        self._introduced_at: dict[str, int] = {}
        self._history: list[
            tuple[CommitGraph, list[Tag], dict[str, str], int, dict[str, int]]
        ] = []
        self._snapshot()

    # ---- 快照 ----

    def _snapshot(self) -> int:
        self._history.append(
            (
                copy.deepcopy(self._graph),
                list(self._tags),
                dict(self._branches),
                self._revision,
                dict(self._introduced_at),
            )
        )
        return len(self._history) - 1

    def snapshot(self) -> int:
        """显式生成快照并返回版本号。"""
        with self._lock:
            return self._snapshot()

    def checkout(self, version: int) -> None:
        """回溯到指定历史版本；版本号越界时拒绝。"""
        with self._lock:
            if not 0 <= version < len(self._history):
                raise PlanError(
                    FailureReason.INVALID_VERSION,
                    f"历史版本 {version} 不存在（共 {len(self._history)} 个快照）",
                )
            g, tags, branches, revision, introduced = self._history[version]
            self._graph = copy.deepcopy(g)
            self._tags = list(tags)
            self._branches = dict(branches)
            self._revision = revision
            self._introduced_at = dict(introduced)

    def history_size(self) -> int:
        with self._lock:
            return len(self._history)

    # ---- 整批写入 ----

    def import_batch(
        self,
        commits: list[Commit] | None = None,
        tags: list[Tag] | None = None,
        branches: dict[str, str] | None = None,
    ) -> BatchResult:
        """整批原子导入提交/标签/分支头。

        任何一条不合法都会在修改前抛出异常，整批不生效。
        """
        commits = list(commits or [])
        tags = list(tags or [])
        branches = dict(branches or {})

        with self._lock:
            # ---- 预校验：提交冲突（批内 + 与存量） ----
            staged: dict[str, Commit] = {}
            for c in commits:
                existing = staged.get(c.id)
                if existing is not None:
                    if existing != c:
                        raise PlanError(
                            FailureReason.DUPLICATE_COMMIT,
                            f"批次内提交 {c.id!r} 重复定义且内容不一致",
                        )
                    continue
                current = self._graph.get(c.id)
                if current is not None and current != c:
                    raise PlanError(
                        FailureReason.DUPLICATE_COMMIT,
                        f"提交 {c.id!r} 已存在且内容不一致",
                    )
                staged[c.id] = c

            def id_known(cid: str) -> bool:
                return cid in staged or self._graph.get(cid) is not None

            # ---- 预校验：标签（冲突、批内重复、目标提交存在） ----
            staged_tags: dict[str, Tag] = {t.name: t for t in self._tags}
            new_tags: list[Tag] = []
            for t in tags:
                prev = staged_tags.get(t.name)
                if prev is not None:
                    if prev.commit_id != t.commit_id:
                        raise PlanError(
                            FailureReason.DUPLICATE_TAG,
                            f"标签 {t.name!r} 同时指向 {prev.commit_id!r} 与 "
                            f"{t.commit_id!r}",
                        )
                    continue
                if not id_known(t.commit_id):
                    raise PlanError(
                        FailureReason.UNKNOWN_TAG,
                        f"标签 {t.name!r} 指向的提交 {t.commit_id!r} 不存在",
                    )
                staged_tags[t.name] = t
                new_tags.append(t)

            # ---- 预校验：分支头提交存在 ----
            for name, head in branches.items():
                if not id_known(head):
                    raise PlanError(
                        FailureReason.UNKNOWN_BRANCH,
                        f"分支 {name!r} 的头提交 {head!r} 不存在",
                    )

            # ---- 全部通过，落盘（此前不得有任何状态修改） ----
            new_ids = [cid for cid, c in staged.items() if self._graph.get(cid) is None]
            for c in staged.values():
                self._graph.add(c)
            for t in new_tags:
                self._tags.append(t)
            changed_branches = 0
            for name, head in branches.items():
                if self._branches.get(name) != head:
                    self._branches[name] = head
                    changed_branches += 1

            if new_ids:
                self._revision += 1
                for cid in sorted(new_ids):
                    self._introduced_at[cid] = self._revision

            changed = bool(new_ids) or bool(new_tags) or changed_branches > 0
            snap = self._snapshot() if changed else len(self._history) - 1

            return BatchResult(
                added_commits=len(new_ids),
                added_tags=len(new_tags),
                changed_branches=changed_branches,
                revision=self._revision,
                snapshot=snap,
            )

    def import_commits(self, commits: list[Commit]) -> int:
        """批量导入提交（整批原子），返回实际新增数量（幂等，重复不计）。"""
        return self.import_batch(commits=commits).added_commits

    def import_records(self, records: list[dict]) -> int:
        """从字典记录导入（旧格式兼容：缺失字段取稳定缺省值）。整批原子。"""
        commits = []
        for r in records:
            if "id" not in r:
                raise PlanError(FailureReason.INVALID_VERSION, "记录缺少必需字段 'id'")
            commits.append(
                Commit(
                    id=r["id"],
                    parents=tuple(r.get("parents", ())),
                    message=r.get("message", ""),
                    branch=r.get("branch", ""),
                    change=r.get("change", "patch"),
                )
            )
        return self.import_commits(commits)

    def add_tag(self, tag: Tag) -> None:
        with self._lock:
            self.import_batch(tags=[tag])

    def set_branch(self, name: str, head: str) -> None:
        with self._lock:
            self.import_batch(branches={name: head})

    # ---- 修订号与增量查询 ----

    def current_revision(self) -> int:
        """当前导入修订号（无任何成功的新提交导入时为 0）。"""
        with self._lock:
            return self._revision

    def _released_ids(self) -> set[str]:
        released: set[str] = set()
        for t in self._tags:
            c = self._graph.get(t.commit_id)
            if c is not None:
                released |= self._graph.ancestors(t.commit_id)
        return released

    def commits_since(self, revision: int) -> IncrementalResult:
        """查询修订号 ``revision`` 之后引入、且当前仍未发布的提交。

        - revision 从 0 开始；查询不存在的修订号抛 REVISION_NOT_FOUND；
        - 结果按 (引入修订号, 提交 id) 排序，跨批次去重，每个提交至多出现一次。
        """
        with self._lock:
            if revision < 0 or revision > self._revision:
                raise PlanError(
                    FailureReason.REVISION_NOT_FOUND,
                    f"修订号 {revision} 不存在（当前为 {self._revision}）",
                )
            released = self._released_ids()
            rows = [
                (rev, cid)
                for cid, rev in self._introduced_at.items()
                if rev > revision and cid not in released
            ]
            rows.sort()
            return IncrementalResult(
                since=revision,
                revision=self._revision,
                commits=[self._graph.get(cid) for _, cid in rows],
            )

    # ---- 读取 ----

    def graph(self) -> CommitGraph:
        with self._lock:
            return copy.deepcopy(self._graph)

    def tags(self) -> list[Tag]:
        with self._lock:
            return list(self._tags)

    def branches(self) -> dict[str, str]:
        with self._lock:
            return dict(self._branches)
