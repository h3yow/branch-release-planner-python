"""多发布线编排：每条线独立算版本与变更日志，并给出跨线统一执行顺序。"""

from __future__ import annotations

from .errors import FailureReason, PlanError
from .graph import CommitGraph
from .models import LinePlan, MultiLinePlan, Tag
from .planner import ReleasePlanner


def plan_lines(
    graph: CommitGraph,
    tags: list[Tag],
    lines: dict[str, dict[str, str]],
) -> MultiLinePlan:
    """对多条发布线分别规划，并给出跨线统一执行顺序。

    lines: {线名: {分支名: 头提交 id}}。

    规则（确定且可解释）：
    - 每条线的版本基线只看自己的历史标签：标签指向的提交必须在该线
      任一分支头的可达历史内，否则对本线而言被忽略，并记入该线的
      ignored_tags 说明原因；
    - 各线的待发布集合、下一版本与变更日志由 ReleasePlanner 在过滤后的
      标签集合上独立计算，互不影响；
    - 跨线统一执行顺序取所有线待发布提交的并集，在整体可达子图上做
      确定性拓扑排序（父先于子，同层按提交 id 字典序），与传分支、
      导提交的先后顺序无关；共享的已发布部分不出现在顺序中。
    """
    rationale: list[str] = []
    if not lines:
        raise PlanError(FailureReason.EMPTY_PLAN, "未提供任何发布线")

    graph.validate()

    # 标签指向的提交必须存在，否则属于非法输入而非“被忽略”
    for t in tags:
        if graph.get(t.commit_id) is None:
            raise PlanError(
                FailureReason.UNKNOWN_TAG,
                f"标签 {t.name!r} 指向的提交 {t.commit_id!r} 不存在",
            )

    result = MultiLinePlan()
    all_heads: list[str] = []
    union_pending: set[str] = set()

    for line_name in sorted(lines):
        branches = lines[line_name]
        reachable: set[str] = set()
        for head in branches.values():
            if graph.get(head) is None:
                raise PlanError(
                    FailureReason.UNKNOWN_BRANCH,
                    f"发布线 {line_name!r} 引用的头提交 {head!r} 不存在",
                )
            reachable |= graph.ancestors(head)

        line_tags: list[Tag] = []
        ignored: list[str] = []
        for t in sorted(tags, key=lambda x: x.name):
            if t.commit_id in reachable:
                line_tags.append(t)
            else:
                ignored.append(
                    f"{t.name}（指向 {t.commit_id}，不在发布线 {line_name} "
                    f"任何分支的可达历史内，对本线版本基线无影响）"
                )
        if ignored:
            rationale.append(
                f"发布线 {line_name}: 忽略 {len(ignored)} 个不属于本线的标签: "
                + ", ".join(ignored)
            )

        plan = ReleasePlanner(graph, line_tags).plan(branches)
        result.lines[line_name] = LinePlan(name=line_name, plan=plan, ignored_tags=ignored)
        all_heads.extend(branches.values())
        union_pending |= set(plan.order)
        rationale.append(
            f"发布线 {line_name}: 基线标签 {[t.name for t in line_tags] or '无'}，"
            f"待发布 {len(plan.order)} 个，下一版本 {plan.next_version}"
        )

    # 跨线统一执行顺序：整体拓扑序过滤出各线待发布并集（共享提交只出现一次）
    full_order = graph.topo_order(all_heads)
    result.global_order = [cid for cid in full_order if cid in union_pending]
    result.rationale = rationale
    result.rationale.append(
        f"跨线统一执行顺序共 {len(result.global_order)} 个提交"
        "（各线待发布并集，拓扑序，同层按 id 字典序，与输入顺序无关）"
    )
    return result
