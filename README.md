# Branch Release Planner（本地分支合并编排与版本发布规划）

以**本地**导入的提交、分支与标签数据为输入，在不依赖任何远端 Git 服务的前提下，给出：

1. 分叉分支的**合并 / 变基执行顺序**；
2. 下一个**语义化版本号**与去重后的**变更日志**；
3. 每个结论附带的**判定依据（reasons）**。

结论由纯函数式的确定性规则产生：无论输入中提交/分支的排列顺序如何、无论操作以何种顺序到达，同一仓库状态下的结论完全一致且可解释。

---

## 1. 数据模型与导入

通过 `POST /import`（或 `PlannerService.import_data`）一次性原子导入：

```json
{
  "commits": [
    {"id": "a", "parents": [], "message": "init", "timestamp": 0},
    {"id": "b", "parents": ["a"], "message": "feat: x", "timestamp": 1}
  ],
  "branches": [{"name": "main", "head": "b"}],
  "tags": [{"name": "v1.0.0", "target": "a"}]
}
```

### 旧格式兼容（字段缺失时的稳定缺省语义）

| 旧字段 | 现行字段 | 缺省值 / 行为 |
| --- | --- | --- |
| `sha` / `hash` | `id` | 缺失则拒绝（`invalid_ref`） |
| `parents` | `parent_ids` | 缺失或缺省为 `[]`（根提交） |
| `msg` | `message` | 缺省为 `""`，不参与 Conventional Commits 判定 |
| `author` | `author` | 缺省为 `""` |
| `timestamp` | `timestamp` | 缺省为 `0`（最旧），仅用于确定性排序 |
| 分支 `target` | `head` | 缺失则拒绝 |
| 标签 `head` | `target` | 缺失则拒绝 |
| 标签 `refs/tags/` 前缀 | — | 自动剥离 |

可选字段缺失永远不会抛错；非法输入（id 为空、父不是列表、引用不存在）会以**可区分错误码**拒绝，不会静默跳过或截断。

### 图校验与失败原因

导入提交后会对**结果图整体**校验，错误不会被吞掉：

| 错误码 `code` | 触发条件 | HTTP |
| --- | --- | --- |
| `duplicate_commit_id` | 同一 id 以**不同内容**重复导入 | 409 |
| `missing_parent_commit` | 父提交在图中不存在（不允许截断） | 422 |
| `cyclic_parent_edge` | 父子边构成有向环（自环、二连环、多连环） | 422 |
| `commit_not_found` / `invalid_ref` | 查询/引用了不存在的提交或分支 | 404 / 400 |
| `invalid_version` | 标签或显式版本号不是合法 SemVer | 422 |
| `no_base_version` | 分支历史上没有任何可达的 SemVer 标签且未显式给 `base_version` | 409 |
| `snapshot_not_found` | 回溯了不存在的历史版本号 | 404 |

注意区分两种“重复”：

* **完全相同的提交再次导入 → 幂等 no-op**，计入响应的 `commits_duplicated`，不产生新版本提升、不重复计入日志；
* **同 id 但内容冲突 → `duplicate_commit_id`**，整批导入回滚，不发布任何中间快照。

---

## 2. 合并 / 变基顺序判定规则（`POST /plan/merge-order`）

入参：`target`（目标分支）、`sources`（待合入分支列表）、`strategy`（`merge` 默认 / `rebase`）。

对每个源分支计算其与目标分支头的 merge-base，并按下述**全序键**排序，因此结论与 `sources` 的输入顺序无关：

1. **已包含优先**：源分支头已经是目标分支祖先时（可 fast-forward）排在最前，动作为 `fast-forward`；
2. **分叉深度**：目标分支头沿一父链回溯到 merge-base 的距离，**距离越大（分叉越旧）越先合并**——先合旧分叉可最大化后续快进机会、缩小冲突面；
3. **源头时间戳**：深度相同时，源分支头时间戳**较新者优先**；
4. **分支名升序**：时间戳也相同时按名字字典序兜底。

`merge` 策略下不可快进的步骤动作是 `merge`；`rebase` 策略下全部步骤动作是 `rebase`，但排序键完全一致。目标分支出现在 sources 中会以 `invalid_ref` 拒绝。

每个步骤返回 `{order, source_branch, merge_base, action}`，merge_base 给出判定依据。

---

## 3. 版本计算规则（`POST /plan/release`）

### 3.1 语义化版本与优先级（SemVer 2.0.0）

* `MAJOR.MINOR.PATCH` 数值比较；
* **构建元数据 `+build` 永不参与优先级比较**（`1.0.0+x == 1.0.0`）；
* 带预发布的版本**低于**同核心的正式版：`1.0.0-rc.1 < 1.0.0`；
* 预发布标识符从左到右比较：纯数字按整数比较且**永远低于**字母数字标识符（`alpha.1 < alpha.beta`，`beta.2 < beta.11`）；前面标识符全同时，标识符列表更长的优先级更高；
* 接受输入标签的 `v`/`V` 前缀，输出版本号不带前缀；数字型预发布标识符禁止前导零（如 `01`）。

### 3.2 基线版本（base version）

* 默认选取分支头历史上**可达标签中优先级最高的 SemVer 标签**；
* **正式标签永远优先于预发布标签**；仅当显式请求预发布（`options.prerelease` 非空）时才把预发布标签纳入候选，此时仍按 SemVer 优先级取最高；
* 非语义化标签（如 `latest`）被忽略并在 reasons 中说明；不可达标签同样说明忽略原因；
* 没有任何可达 SemVer 标签时必须显式传 `options.base_version`，否则返回 `no_base_version`，绝不臆造版本。

### 3.3 Bump 级别

统计基线点之后、分支头可达的**非合并提交**的 message：

| 规则（取最高级） | bump |
| --- | --- |
| `BREAKING CHANGE` / `BREAKING-CHANGE` 正文，或 `feat!:`/`fix!:` 等 `!` 标记 | `major` |
| `feat(scope?):` | `minor` |
| `fix(scope?):` | `patch` |
| 其他 Conventional Commits（docs/chore/ci…）或非规范 message | 不强制提升 |

没有任何规则命中时使用 `options.default_bump`（默认 `patch`）。预发布通过 `options.prerelease`（如 `"rc"`）构建，基线类型决定结果：

* 基线为正式版（如 `1.0.0`）→ 在 bump 后的核心上开启新预发布线：`1.1.0-rc.1`；
* 基线为**同标识**预发布（如已有 `1.1.0-rc.1`）→ 计数器递增、核心不变：`1.1.0-rc.2`（`rc.9 -> rc.10`）；
* 基线为**异标识**预发布（如 `1.1.0-beta.2` 上构建 `rc`）→ 在同一核心上从 1 重新计数：`1.1.0-rc.1`；
* 构建元数据始终从结果中剥离。

### 3.4 去重与确定性日志

* 菱形历史中同一提交可能沿多条父边走到，日志按提交 id **只计一次**；
* **合并提交（≥2 个父）不进日志**，但在 reasons 中记录跳过；
* 日志固定按 `(timestamp 降序, id 升序)` 排列；
* 幂等重复导入不会让同一提交再次进入日志或再次提升版本。

响应包含 `next_version / bump / base_version / changelog / reasons / revision`，`reasons` 逐条说明每个提交的判定、标签取舍与最终 bump 计算式。

---

## 4. 并发、幂等与历史回溯

`Repository` 以一把可重入锁保护，每次变更都在临界区内完成“读-改-校验-发布”：

* **并发导入不丢变更**：多线程批量导入，最终图 = 初始图 ∪ 所有批次，`commits_added` 汇总等于实际新增数；
* **原子性**：任何一批数据校验失败（如缺失父、环、同 id 冲突），整批不生效，不发布半成品快照，revision 不前进；
* **只追加不可变快照**：每次成功变更生成 revision 单调递增的 `StateSnapshot`，历史快照永不修改；
* **可复现回溯**：所有规划接口接受 `revision`，`GET /state?revision=n`、`/plan/merge-order`、`/plan/release` 都可针对任一历史快照计算，重复读取同一 revision 结论必然一致；`GET /history` 列出全部 revision。

---

## 5. HTTP API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查与当前 revision |
| POST | `/import` | 原子导入 commits/branches/tags |
| POST | `/plan/merge-order` | 合并/变基顺序 |
| POST | `/plan/release` | 下一版本 + 变更日志 + 判定依据 |
| GET | `/state?revision=n` | 查看当前或历史状态 |
| GET | `/history` | revision 列表 |

错误体统一为 `{"error": {"code", "message", "details"}}`，按上表区分 HTTP 状态码与业务错误码。

---

## 6. 本地验证方法

前置：Python 3.14+、[uv](https://docs.astral.sh/uv)。

```bash
# 安装依赖（含 dev 测试组）
uv sync --group dev

# 运行全部测试（-s 已配置，日志会打印每个用例的输入数据与判定依据）
uv run pytest -s

# 仅跑某一类场景
uv run pytest tests/test_planner.py -s        # 分叉合并 / 版本提升 / 预发布排序 / 去重
uv run pytest tests/test_graph.py -s          # 分叉合并 / 循环 / 缺失父 / 重复导入
uv run pytest tests/test_repository.py -s     # 并发写 / 幂等 / 快照回溯 / 旧格式
uv run pytest tests/test_concurrency_stress.py -s

# 编译检查
uv run python -m compileall -q branch_release_planner main.py tests

# 启动服务
uv run python main.py        # 或: uv run uvicorn branch_release_planner.api:app --reload
```

手工 smoke：

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/import -H 'content-type: application/json' -d '{
  "commits": [
    {"id":"a","parents":[],"message":"init","timestamp":0},
    {"id":"b","parents":["a"],"message":"feat: x","timestamp":1}
  ],
  "branches":[{"name":"main","head":"b"}],
  "tags":[{"name":"v1.0.0","target":"a"}]
}'
curl -s -X POST localhost:8000/plan/release -H 'content-type: application/json' \
  -d '{"branch":"main"}'
```
