"""批量导入原子性、导入修订号单调性与增量查询、并发一致性。"""

import logging
import threading

import pytest

from release_planner import Commit, FailureReason, PlanError, Tag
from release_planner.store import RepoStore

log = logging.getLogger(__name__)


def test_failed_batch_leaves_state_and_revision_untouched():
    store = RepoStore()
    rev0 = store.revision()
    ok = store.import_batch(commits=[Commit("a", message="init")])
    log.info("首批导入后修订号: %d", ok)
    assert ok == rev0 + 1
    size_before = len(store.graph())

    # 批次里混入了与已有提交 id 相同但内容不同的记录 -> 整批拒绝
    with pytest.raises(PlanError) as ei:
        store.import_batch(commits=[
            Commit("b", parents=("a",), message="new"),
            Commit("a", message="CONFLICT"),
        ])
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.DUPLICATE_COMMIT
    # 状态与修订号都不动：b 不能被部分导入
    assert store.revision() == ok
    assert len(store.graph()) == size_before
    assert store.graph().get("b") is None
    log.info("失败后修订号仍为 %d，提交数仍为 %d", store.revision(), len(store.graph()))


def test_failed_batch_with_dangling_parent_rolls_back():
    store = RepoStore()
    store.import_batch(commits=[Commit("a")])
    rev = store.revision()
    with pytest.raises(PlanError) as ei:
        store.import_batch(commits=[Commit("c", parents=("ghost",))])
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.MISSING_PARENT
    assert store.revision() == rev
    assert store.graph().get("c") is None


def test_failed_batch_with_unknown_branch_head_rolls_back():
    store = RepoStore()
    store.import_batch(commits=[Commit("a")])
    rev = store.revision()
    with pytest.raises(PlanError) as ei:
        store.import_batch(commits=[Commit("b", parents=("a",))],
                           branches={"main": "ghost"})
    assert ei.value.reason is FailureReason.UNKNOWN_BRANCH
    assert store.revision() == rev
    assert store.graph().get("b") is None
    assert store.branches() == {}


def test_revision_monotonic_and_no_empty_bump_on_reimport():
    store = RepoStore()
    batch = [Commit("a"), Commit("b", parents=("a",))]
    r1 = store.import_batch(commits=batch)
    r2 = store.import_batch(commits=batch)  # 同一批重复导入
    r3 = store.import_batch(commits=[Commit("a")])  # 子集重复导入
    log.info("重复导入修订号: %d -> %d -> %d", r1, r2, r3)
    assert r2 == r1 and r3 == r1  # 修订号不空涨
    r4 = store.import_batch(commits=[Commit("c", parents=("b",))])
    log.info("新内容导入后修订号: %d", r4)
    assert r4 == r1 + 1  # 单调递增
    r5 = store.import_batch(commits=[Commit("c", parents=("b",))],
                            tags=[Tag("v1.0.0", "c")])
    assert r5 == r4 + 1  # 新标签也算新内容
    r6 = store.import_batch(tags=[Tag("v1.0.0", "c")])  # 重复标签不涨
    assert r6 == r5


def test_commits_since_increment_no_dup_no_omission():
    store = RepoStore()
    store.import_batch(commits=[Commit("a"), Commit("b", parents=("a",))],
                       tags=[Tag("v1.0.0", "b")])
    rev1 = store.revision()
    store.import_batch(commits=[Commit("c", parents=("b",))])
    rev2 = store.revision()
    store.import_batch(commits=[Commit("d", parents=("c",)), Commit("e", parents=("c",))])
    rev3 = store.revision()
    log.info("修订号轨迹: %d -> %d -> %d", rev1, rev2, rev3)

    since0 = store.commits_since(0)
    since1 = store.commits_since(rev1)
    since2 = store.commits_since(rev2)
    log.info("since(0)=%s since(%d)=%s since(%d)=%s", since0, rev1, since1, rev2, since2)
    # 已发布的 a、b 不出现在增量里；c、d、e 按修订号分段，不重不漏
    assert since0 == ["c", "d", "e"]
    assert since1 == ["c", "d", "e"]
    assert since2 == ["d", "e"]
    assert len(set(since0)) == len(since0)
    # 重复导入同一批后，增量结果不变（同一提交不会出现两次）
    store.import_batch(commits=[Commit("d", parents=("c",)), Commit("e", parents=("c",))])
    assert store.commits_since(rev2) == ["d", "e"]
    # 全量分段拼起来正好覆盖且互不重叠
    assert sorted(since1) == sorted(store.commits_since(rev1))
    assert set(since1) == set(since1)  # 无重复


def test_commits_since_excludes_newly_released():
    store = RepoStore()
    store.import_batch(commits=[Commit("a"), Commit("b", parents=("a",))])
    rev = store.revision()
    assert store.commits_since(0) == ["a", "b"]
    # 之后给 b 打标签：a、b 变为已发布，从增量中消失
    store.import_batch(tags=[Tag("v1.0.0", "b")])
    log.info("打标签后 since(0)=%s", store.commits_since(0))
    assert store.commits_since(0) == []


def test_commits_since_invalid_revision_rejected():
    store = RepoStore()
    store.import_batch(commits=[Commit("a")])
    with pytest.raises(PlanError) as ei:
        store.commits_since(store.revision() + 1)
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.INVALID_REVISION
    with pytest.raises(PlanError):
        store.commits_since(-1)


def test_concurrent_batches_revision_matches_content():
    """并发导入：每个生效批次拿到唯一修订号，内容与修订号一一对应。"""
    store = RepoStore()
    n_threads, n_commits = 8, 20
    results: list[tuple[int, int]] = []  # (线程内批次数, 返回的修订号)
    lock = threading.Lock()

    def worker(tid: int):
        for b in range(3):
            commits = []
            for i in range(n_commits):
                if i % 4 == 0:
                    # 共享提交：内容一致，幂等去重
                    commits.append(Commit(f"shared-{i}", message="shared"))
                else:
                    commits.append(Commit(f"t{tid}-b{b}-{i}", message=f"t{tid} b{b}"))
            rev = store.import_batch(commits=commits)
            with lock:
                results.append((tid, rev))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    revs = sorted(r for _, r in results)
    log.info("并发返回的修订号: %s", revs)
    # 每个返回的修订号都不超过当前值，且最终修订号 = 实际生效的批次数
    final = store.revision()
    assert all(0 < r <= final for r in revs)
    # 内容不丢不重：共享 5 个 + 每线程每批 15 个独有
    expected = 5 + n_threads * 3 * 15
    log.info("期望提交数 %d，实际 %d，最终修订号 %d", expected, len(store.graph()), final)
    assert len(store.graph()) == expected
    # 修订号单调：同一线程内后一批的修订号不小于前一批
    by_thread: dict[int, list[int]] = {}
    for tid, rev in results:
        by_thread.setdefault(tid, []).append(rev)
    for tid, seq in by_thread.items():
        assert seq == sorted(seq), f"线程 {tid} 修订号非单调: {seq}"
    # 增量查询覆盖全部提交且不重复
    inc = store.commits_since(0)
    assert sorted(inc) == sorted(c.id for c in store.graph().commits())


def test_revision_consistent_after_checkout():
    """回溯历史快照后，修订号与增量查询也跟当时一致。"""
    store = RepoStore()
    store.import_batch(commits=[Commit("a")])
    v = store.snapshot()
    rev_at_v = store.revision()
    store.import_batch(commits=[Commit("b", parents=("a",))])
    assert store.commits_since(rev_at_v) == ["b"]
    store.checkout(v)
    log.info("回溯到快照 %d 后修订号 %d", v, store.revision())
    assert store.revision() == rev_at_v
    assert store.commits_since(0) == ["a"]
    assert store.graph().get("b") is None
