"""规划器：合并顺序、下一版本号与变更日志。"""

from __future__ import annotations

from .errors import FailureReason, PlanError
from .graph import CommitGraph
from .models import MergePlan, SemVer, Tag

_LEVEL_RANK = {"none": 0, "patch": 1, "minor": 2, "major": 3}


class ReleasePlanner:
    """在给定分支与标签集合上计算发布计划。

    规则（确定且可解释）：
    - 基线版本：所有标签按 semver 排序（构建元数据不参与比较）取最高者；
      无标签时基线为 0.0.0。
    - 待发布提交：从各分支头可达、且不属于任何标签提交祖先集合的提交。
      重复导入同一提交只会在图中出现一次，因此不会重复计入日志或重复提升版本。
    - 合并顺序：待发布子图的拓扑序（父先于子），同层按提交 id 字典序，
      保证与操作顺序无关。
    - 版本提升：取待发布提交中的最高变更级别（major > minor > patch > none），
      在基线版本上提升一次；提升后清除预发布段与构建元数据。
    """

    def __init__(self, graph: CommitGraph, tags: list[Tag] | None = None) -> None:
        self._graph = graph
        self._tags = list(tags or [])

    def _baseline(self, rationale: list[str]) -> SemVer:
        versions: list[tuple[SemVer, str]] = []
        for t in self._tags:
            v = SemVer.parse(t.name)  # 无法解析时抛 INVALID_VERSION
            versions.append((v, t.name))
        if not versions:
            rationale.append("无历史标签，基线版本取 0.0.0")
            return SemVer(0, 0, 0)
        versions.sort(key=lambda x: x[0])
        top_v, top_name = versions[-1]
        rationale.append(
            "标签按 semver 排序（构建元数据不参与比较）："
            + " < ".join(name for _, name in versions)
        )
        rationale.append(f"基线版本取最高标签 {top_name} -> {top_v}")
        return top_v

    def plan(self, branches: dict[str, str]) -> MergePlan:
        rationale: list[str] = []
        if not branches:
            raise PlanError(FailureReason.EMPTY_PLAN, "未提供任何分支")

        self._graph.validate()

        heads: dict[str, str] = {}
        for name, head in sorted(branches.items()):
            if self._graph.get(head) is None:
                raise PlanError(
                    FailureReason.UNKNOWN_BRANCH,
                    f"分支 {name!r} 的头提交 {head!r} 不存在",
                )
            heads[name] = head
        rationale.append(
            "参与规划的分支: " + ", ".join(f"{n}@{h}" for n, h in heads.items())
        )

        # 已发布集合：所有标签提交的祖先
        released: set[str] = set()
        for t in self._tags:
            if self._graph.get(t.commit_id) is None:
                raise PlanError(
                    FailureReason.UNKNOWN_TAG,
                    f"标签 {t.name!r} 指向的提交 {t.commit_id!r} 不存在",
                )
            released |= self._graph.ancestors(t.commit_id)

        # 待发布集合：分支头可达但未发布
        reachable: set[str] = set()
        for h in heads.values():
            reachable |= self._graph.ancestors(h)
        pending = reachable - released
        rationale.append(
            f"可达提交 {len(reachable)} 个，已发布 {len(released)} 个，"
            f"待发布 {len(pending)} 个"
        )

        # 分叉点信息（可解释性）
        head_list = list(heads.values())
        for i in range(len(head_list)):
            for j in range(i + 1, len(head_list)):
                base = self._graph.merge_base(head_list[i], head_list[j])
                if base:
                    rationale.append(
                        f"分支头 {head_list[i]!r} 与 {head_list[j]!r} 的分叉点为 {base!r}"
                    )

        # 确定性拓扑序，过滤出待发布提交
        full_order = self._graph.topo_order(head_list)
        order = [cid for cid in full_order if cid in pending]
        rationale.append("合并顺序按拓扑序（父先于子），同层按提交 id 字典序")

        # 变更日志：按执行顺序收集提交消息（提交在图中唯一，天然去重）
        changelog = [
            f"{cid}: {self._graph.get(cid).message or '(无消息)'}" for cid in order
        ]

        # 版本提升
        base = self._baseline(rationale)
        level = "none"
        for cid in order:
            c = self._graph.get(cid)
            if _LEVEL_RANK.get(c.change, 1) > _LEVEL_RANK[level]:
                level = c.change if c.change in _LEVEL_RANK else "patch"
        if not order:
            rationale.append("无待发布提交，版本保持基线不变")
            next_version = base
        elif base.prerelease:
            # 预发布毕业规则：基线仍是预发布版，说明该核心版本尚未正式发布，
            # 待发布变更并入这一未完成的发布，下一版本为其正式版（核心号）。
            next_version = SemVer(base.major, base.minor, base.patch)
            rationale.append(
                f"基线 {base} 为预发布版本，待发布变更并入未完成的发布，"
                f"下一版本毕业为正式版 {next_version}"
            )
        else:
            rationale.append(f"待发布提交最高变更级别为 {level}")
            next_version = base.bump(level)
            rationale.append(f"下一版本: {base} --{level}--> {next_version}")

        return MergePlan(
            order=order,
            next_version=next_version,
            changelog=changelog,
            rationale=rationale,
        )
