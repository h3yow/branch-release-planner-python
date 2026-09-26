"""存储：并发写入不丢失不重复、历史回溯可复现、旧格式缺省兼容。"""

import logging
import threading

from release_planner import Commit, Tag
from release_planner.planner import ReleasePlanner
from release_planner.store import RepoStore

log = logging.getLogger(__name__)


def test_concurrent_imports_no_loss_no_duplication():
    store = RepoStore()
    # 8 个线程各导入 50 个提交，其中一半与其他线程重叠
    def worker(tid: int):
        commits = []
        for i in range(50):
            if i % 2 == 0:
                # 共享提交：各线程内容完全一致，应被幂等去重
                commits.append(Commit(f"shared-{i % 25}", message="shared"))
            else:
                commits.append(Commit(f"t{tid}-{i}", message=f"from thread {tid}"))
        return store.import_commits(commits)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    g = store.graph()
    expected = 25 + 8 * 25  # 25 个共享 + 每线程 25 个独有
    log.info("并发导入后期望 %d 个提交，实际 %d 个", expected, len(g))
    assert len(g) == expected  # 无丢失、无重复


def test_snapshot_checkout_reproducible():
    store = RepoStore()
    store.import_commits([
        Commit("a", message="init", change="major"),
        Commit("b", parents=("a",), message="feat", change="minor"),
    ])
    store.add_tag(Tag("v1.0.0", "a"))
    store.set_branch("main", "b")

    def current_plan():
        p = ReleasePlanner(store.graph(), store.tags())
        return p.plan(store.branches())

    plan_before = current_plan()
    v = store.snapshot()
    log.info("快照 v%d 时结论: order=%s next=%s", v, plan_before.order, plan_before.next_version)

    # 继续写入改变状态
    store.import_commits([Commit("c", parents=("b",), message="break", change="major")])
    store.set_branch("main", "c")
    plan_after = current_plan()
    log.info("写入后结论: order=%s next=%s", plan_after.order, plan_after.next_version)
    assert plan_after.order != plan_before.order

    # 回溯后结论必须与当时完全一致
    store.checkout(v)
    plan_replayed = current_plan()
    log.info("回溯后结论: order=%s next=%s", plan_replayed.order, plan_replayed.next_version)
    assert plan_replayed.order == plan_before.order
    assert plan_replayed.next_version == plan_before.next_version
    assert plan_replayed.changelog == plan_before.changelog


def test_legacy_records_default_semantics():
    store = RepoStore()
    # 旧格式记录：只有 id，缺省字段需稳定
    store.import_records([{"id": "a"}, {"id": "b", "parents": ["a"]}])
    g = store.graph()
    b = g.get("b")
    log.info("旧格式记录导入结果: b.parents=%s change=%s message=%r", b.parents, b.change, b.message)
    assert b.parents == ("a",)
    assert b.change == "patch"     # 缺省变更级别
    assert b.message == ""         # 缺省消息
    assert b.branch == ""          # 缺省分支
