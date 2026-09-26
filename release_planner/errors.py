"""失败原因定义：所有可区分的拒绝原因集中在此。"""

from __future__ import annotations

import enum


class FailureReason(enum.Enum):
    """输入无法给出合法结论时的可区分失败原因。"""

    DUPLICATE_COMMIT = "duplicate_commit"          # 同一提交被重复定义（id 冲突且内容不同）
    MISSING_PARENT = "missing_parent"              # 父提交在图中缺失
    CYCLIC_PARENT_EDGE = "cyclic_parent_edge"      # 父边构成环
    UNKNOWN_BRANCH = "unknown_branch"              # 引用了不存在的分支
    UNKNOWN_TAG = "unknown_tag"                    # 引用了不存在的标签
    INVALID_VERSION = "invalid_version"            # 版本号无法解析
    EMPTY_PLAN = "empty_plan"                      # 无可规划的提交


class PlanError(Exception):
    """携带失败原因的领域异常。"""

    def __init__(self, reason: FailureReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")
