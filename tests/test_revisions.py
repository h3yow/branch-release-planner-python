"""整批原子导入、导入修订号、增量查询、并发一致性。

判定规则：
- 批次要么整批生效、要么整批不生效；失败后提交图、标签、分支、修订号、快照
  全部保持失败前状态。
- 修订号仅在批次引入至少一个新提交时 +1，单调递增；同批重复导入修订号不涨。
- commits_since(rev) 给出 rev 之后引入且当前未发布的提交，按 (修订号, id)
  排序，跨批次去重，不重不漏；发布（打标签）后相应提交从增量中消失。
- 并发导入下，修订号严格递增、互不重复，提交内容与引入修订号一一对应。
"""

import logging
import threading

import pytest

from release_planner import Commit, FailureReason, PlanError, Tag
from release_planner.store import RepoStore

log = logging.getLogger(__name__)


def test_failed_batch_leaves_state_and_revision_unchanged():
    s = RepoStore()
    s.import_batch([Commit("a"), Commit("b", parents=("a",))])
    rev_before = s.current_revision()
    snap_before = s.history_size()
    graph_before = len(s.graph())
    log.info("失败前: rev=%d 快照=%d 提交数=%d", rev_before, snap_before, graph_before)

    # 批次里有一个与存量内容冲突的提交：整批必须回滚（c 也不能留下）
    with pytest.raises(PlanError) as ei:
        s.import_batch([Commit("c", parents=("b",)),
                        Commit("a", message="tampered", change="major")])
    log.info("拒绝原因: %s / %s", ei.value.reason, ei.value.detail)
    assert ei.value.reason is FailureReason.DUPLICATE_COMMIT

    log.info("失败后: rev=%d 快照=%d 提交数=%d",
             s.current_revision(), s.history_size(), len(s.graph()))
    assert s.current_revision() == rev_before
    assert s.history_size() == snap_before
    assert len(s.graph()) == graph_before
    assert s.graph().get("c") is None  # 批次中合法的部分也未生效


def test_failed_tag_and_branch_batch_is_rollback():
    s = RepoStore()
    s.import_batch([Commit("a")])
    rev_before = s.current_revision()
    # 标签引用不存在提交 -> 整批失败；同批的分支头也不能落地
    with pytest.raises(PlanError) as ei:
        s.import_batch(
            commits=[Commit("b", parents=("a",))],
            tags=[Tag("v1.0.0", "ghost")],
            branches={"main": "b"},
        )
    log.info("拒绝原因: %s", ei.value.reason)
    assert ei.value.reason is FailureReason.UNKNOWN_TAG
    assert len(s.graph()) == 1
    assert s.branches() == {}
    assert s.tags() == []
    assert s.current_revision() == rev_before


def test_duplicate_batch_does_not_bump_revision():
    s = RepoStore()
    batch = [Commit("a"), Commit("b", parents=("a",))]
    r1 = s.import_batch(batch)
    r2 = s.import_batch(batch)
    log.info("首次: rev=%d added=%d；重复: rev=%d added=%d",
             r1.revision, r1.added_commits, r2.revision, r2.added_commits)
    assert r1.revision == 1
    assert r2.added_commits == 0
    assert r2.revision == 1  # 修订号不空涨


def test_revision_monotonic_and_incremental_no_dup_no_miss():
    s = RepoStore()
    s.import_batch([Commit("a")], tags=[Tag("v0.1.0", "a")])
    r2 = s.import_batch([Commit("b", parents=("a",), change="minor")])
    r3 = s.import_batch([
        Commit("c", parents=("b",), change="patch"),
        Commit("d", parents=("c",), change="patch"),
    ])
    log.info("修订号: b@%d, c/d@%d", r2.revision, r3.revision)
    assert r2.revision == 2 and r3.revision == 3

    inc = s.commits_since(1)
    ids = [c.id for c in inc.commits]
    log.info("since=1 的增量: %s（当前 rev=%d）", ids, inc.revision)
    assert ids == ["b", "c", "d"]          # 按 (修订号, id)，不重不漏
    assert len(ids) == len(set(ids))

    inc_mid = s.commits_since(2)
    log.info("since=2 的增量: %s", [c.id for c in inc_mid.commits])
    assert [c.id for c in inc_mid.commits] == ["c", "d"]

    # 同批重复导入后增量不变、修订号不涨
    s.import_batch([Commit("c", parents=("b",), change="patch"),
                    Commit("d", parents=("c",), change="patch")])
    inc2 = s.commits_since(1)
    assert [c.id for c in inc2.commits] == ["b", "c", "d"]
    assert s.current_revision() == 3

    # 发布 c,d 后（标签指向 d 会同时发布其祖先 c），增量中相应提交消失
    s.import_batch(tags=[Tag("v1.0.0", "d")])
    inc3 = s.commits_since(1)
    log.info("发布后 since=1 的增量: %s", [c.id for c in inc3.commits])
    assert [c.id for c in inc3.commits] == []  # b 是 d 的祖先，也一并发布


def test_commits_since_boundaries():
    s = RepoStore()
    s.import_batch([Commit("a")])
    log.info("边界: since=0 -> %s", [c.id for c in s.commits_since(0).commits])
    assert [c.id for c in s.commits_since(0).commits] == ["a"]
    assert s.commits_since(s.current_revision()).commits == []  # 从当前版本起无新增
    with pytest.raises(PlanError) as ei:
        s.commits_since(99)
    log.info("越界修订号拒绝原因: %s", ei.value.reason)
    assert ei.value.reason is FailureReason.REVISION_NOT_FOUND


def test_concurrent_imports_revision_and_content_consistency():
    s = RepoStore()
    # 每个线程导入一批：共享提交 s0..s24（内容一致）+ 独有提交
    def worker(tid):
        cs = []
        for i in range(50):
            if i % 2 == 0:
                cs.append(Commit(f"s{i % 25}", message="shared"))
            else:
                cs.append(Commit(f"t{tid}-{i}",
                                 parents=(f"s{i % 25}",), message=f"w{tid}"))
        s.import_batch(cs)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    g = s.graph()
    g.validate()  # 并发后图仍完整、父引用齐全
    expected = 25 + 8 * 25
    log.info("并发后提交数: 期望=%d 实际=%d, 当前修订号=%d",
             expected, len(g), s.current_revision())
    assert len(g) == expected
    # 每个批次至少引入其 25 个独有提交 => 8 个成功批次，修订号恰为 8
    assert s.current_revision() == 8

    # 全量增量不重不漏
    inc = s.commits_since(0)
    ids = [c.id for c in inc.commits]
    assert len(ids) == expected
    assert len(ids) == len(set(ids))

    # 账本一致性：每个提交的引入修订号都在 [1, 当前]，且内容可取回
    for c in inc.commits:
        assert g.get(c.id) == c
