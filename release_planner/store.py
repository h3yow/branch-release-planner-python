"""仓库状态存储：原子批量导入、导入修订号、并发读写与历史快照回溯。"""

from __future__ import annotations

import copy
import threading

from .errors import FailureReason, PlanError
from .graph import CommitGraph
from .models import Commit, Tag


class RepoStore:
    """线程安全的仓库状态，支持原子批量导入、导入修订号与历史快照回溯。

    - 所有写操作在互斥锁内完成，并发导入不会丢失或重复应用变更
      （提交按 id 幂等去重，重复导入返回 0 新增）。
    - 批量导入是原子的：先在副本上应用整批，任一记录非法则整批拒绝，
      仓库状态与导入修订号都保持不变。
    - 导入修订号单调递增，且仅在批次真正引入新内容时递增；
      重复导入同一批数据不会让修订号空涨。
    - 每次成功的写批次自动生成一个不可变快照（含修订号）；checkout 到任一
      历史版本后，规划结论与当时完全一致（可复现）。
    - import_records 兼容旧格式：字段缺失时使用稳定缺省值
      （parents=()、message=""、branch=""、change="patch"）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._graph = CommitGraph()
        self._tags: list[Tag] = []
        self._branches: dict[str, str] = {}
        self._revision = 0
        # 每个提交首次导入时的修订号，用于增量查询
        self._commit_revisions: dict[str, int] = {}
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
                dict(self._commit_revisions),
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
            g, tags, branches, rev, crevs = self._history[version]
            self._graph = copy.deepcopy(g)
            self._tags = list(tags)
            self._branches = dict(branches)
            self._revision = rev
            self._commit_revisions = dict(crevs)

    def history_size(self) -> int:
        with self._lock:
            return len(self._history)

    # ---- 修订号 ----

    def revision(self) -> int:
        """当前导入修订号（单调递增，仅在新内容生效时前进）。"""
        with self._lock:
            return self._revision

    # ---- 写入 ----

    def _apply_commits(
        self,
        graph: CommitGraph,
        commit_revisions: dict[str, int],
        commits: list[Commit],
    ) -> int:
        """在给定图副本上应用提交，返回新增数量；冲突时抛错，调用方丢弃副本。"""
        added = 0
        for c in commits:
            if graph.add(c):  # 幂等去重；id 冲突抛 DUPLICATE_COMMIT
                commit_revisions[c.id] = self._revision + 1
                added += 1
        return added

    def import_commits(self, commits: list[Commit]) -> int:
        """批量导入提交（原子），返回实际新增数量（幂等，重复不计）。

        任一提交与已有内容冲突时整批拒绝，状态与修订号均不变。
        """
        with self._lock:
            graph = copy.deepcopy(self._graph)
            crevs = dict(self._commit_revisions)
            added = self._apply_commits(graph, crevs, commits)
            if added:
                self._graph = graph
                self._commit_revisions = crevs
                self._revision += 1
                self._snapshot()
            return added

    def import_batch(
        self,
        commits: list[Commit] | None = None,
        tags: list[Tag] | None = None,
        branches: dict[str, str] | None = None,
    ) -> int:
        """原子批量导入提交、标签与分支，返回生效后的导入修订号。

        整批先在副本上应用并校验（提交冲突、父提交缺失/成环、标签或分支
        指向不存在的提交），任一失败则整批不生效，状态与修订号保持原值。
        批次未带来任何新内容时修订号不前进。
        """
        commits = list(commits or [])
        tags = list(tags or [])
        branches = dict(branches or {})
        with self._lock:
            graph = copy.deepcopy(self._graph)
            crevs = dict(self._commit_revisions)
            new_tags = list(self._tags)
            new_branches = dict(self._branches)

            added = self._apply_commits(graph, crevs, commits)
            graph.validate()  # 父提交缺失 / 循环父边整批拒绝

            changed = added > 0
            for t in tags:
                if graph.get(t.commit_id) is None:
                    raise PlanError(
                        FailureReason.UNKNOWN_TAG,
                        f"标签 {t.name!r} 指向的提交 {t.commit_id!r} 不存在",
                    )
                if t not in new_tags:
                    new_tags.append(t)
                    changed = True
            for name, head in sorted(branches.items()):
                if graph.get(head) is None:
                    raise PlanError(
                        FailureReason.UNKNOWN_BRANCH,
                        f"分支 {name!r} 的头提交 {head!r} 不存在",
                    )
                if new_branches.get(name) != head:
                    new_branches[name] = head
                    changed = True

            if changed:
                self._graph = graph
                self._commit_revisions = crevs
                self._tags = new_tags
                self._branches = new_branches
                self._revision += 1
                self._snapshot()
            return self._revision

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
                self._revision += 1
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
                self._revision += 1
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

    def commits_since(self, revision: int) -> list[str]:
        """从指定修订号到现在新增、且尚未发布的提交 id（拓扑序，不重不漏）。

        “已发布”指属于任一标签提交的祖先集合。同一提交只会在其首次导入的
        修订号上计入一次，重复导入不会让它再次出现在增量结果里。
        """
        with self._lock:
            if not 0 <= revision <= self._revision:
                raise PlanError(
                    FailureReason.INVALID_REVISION,
                    f"修订号 {revision} 越界（当前为 {self._revision}）",
                )
            released: set[str] = set()
            for t in self._tags:
                if self._graph.get(t.commit_id) is not None:
                    released |= self._graph.ancestors(t.commit_id)
            full = self._graph.topo_order([c.id for c in self._graph.commits()])
            return [
                cid
                for cid in full
                if self._commit_revisions.get(cid, 0) > revision
                and cid not in released
            ]
