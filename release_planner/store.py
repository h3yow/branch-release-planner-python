"""仓库状态存储：并发读写与历史快照回溯。"""

from __future__ import annotations

import copy
import threading

from .errors import FailureReason, PlanError
from .graph import CommitGraph
from .models import Commit, Tag


class RepoStore:
    """线程安全的仓库状态，支持历史快照回溯。

    - 所有写操作在互斥锁内完成，并发导入不会丢失或重复应用变更
      （提交按 id 幂等去重，重复导入返回 0 新增）。
    - 每次成功的写批次自动生成一个不可变快照；checkout 到任一历史
      版本后，规划结论与当时完全一致（可复现）。
    - import_records 兼容旧格式：字段缺失时使用稳定缺省值
      （parents=()、message=""、branch=""、change="patch"）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._graph = CommitGraph()
        self._tags: list[Tag] = []
        self._branches: dict[str, str] = {}
        self._history: list[tuple[CommitGraph, list[Tag], dict[str, str]]] = []
        self._snapshot()

    # ---- 快照 ----

    def _snapshot(self) -> int:
        self._history.append(
            (copy.deepcopy(self._graph), list(self._tags), dict(self._branches))
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
            g, tags, branches = self._history[version]
            self._graph = copy.deepcopy(g)
            self._tags = list(tags)
            self._branches = dict(branches)

    def history_size(self) -> int:
        with self._lock:
            return len(self._history)

    # ---- 写入 ----

    def import_commits(self, commits: list[Commit]) -> int:
        """批量导入提交，返回实际新增数量（幂等，重复不计）。"""
        added = 0
        with self._lock:
            for c in commits:
                if self._graph.add(c):
                    added += 1
            if added:
                self._snapshot()
        return added

    def import_records(self, records: list[dict]) -> int:
        """从字典记录导入（旧格式兼容：缺失字段取稳定缺省值）。"""
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
            if tag not in self._tags:
                self._tags.append(tag)
                self._snapshot()

    def set_branch(self, name: str, head: str) -> None:
        with self._lock:
            if self._graph.get(head) is None:
                raise PlanError(
                    FailureReason.UNKNOWN_BRANCH,
                    f"分支 {name!r} 的头提交 {head!r} 不存在",
                )
            if self._branches.get(name) != head:
                self._branches[name] = head
                self._snapshot()

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
