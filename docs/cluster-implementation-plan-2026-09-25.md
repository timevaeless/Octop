# Octop 集群化剩余工作实施计划

> 编制日期：2026-09-25 ｜ 分支：`feat/cluster-stage3`（HEAD `e603705f`）
> 工作目录：`/Users/timevaeless/Documents/Work/Octop-cluster`
> 输入文档：
> - `HANDOFF.md`（阶段收尾，列 10 项待修）
> - `docs/cluster-assessment-2026-09-25.md`（独立评估，5 项独立实测）
> - `docs/cluster-review-2026-09-25.md`（已归档三轮评审）
> 本文档**不改动源码**，仅作为后续逐项编码的依据。所有 `文件:行号` 均经逐行回查确认。

---

## 0. 一句话定位

**机制层已成型且核实生效；剩余工作是"堵洞 → 补覆盖面 → 对齐文档"三类收口，不含机制重做。**

集群已能跑（6 提交链、46 单测、`make all` 全绿），但三个红色缺陷会在真实负载下咬人：长回合双跑、渠道改了不生效、api 崩了续不上。

---

## 1. 当前状态（已成型、已逐行核实）

| 机制 | 实现位置 | 核实结论 |
|---|---|---|
| 选主（会话级 advisory lock） | `infra/cluster/leader.py` | ✅ 优于 TTL 租约：进程死=连接断=锁立即释放，无脑裂窗口 |
| 半开连接兜底 | `leader.py:41-44`（keepalive idle=30s/interval=10s/count=3）、`:172-173`（`SELECT 1` 带 `wait_for` 超时） | ✅ 接管延迟压到秒级 |
| 过渡串行化 | `leader.py:87,89,124-126,204-213`（`_transition_lock`+`_handles_running`） | ✅ stop 恰好一次，有对应单测 |
| `BackgroundGate` 第三态 | `leader.py:253,271,284`（`enabled` 标志，worker 角色直接 return） | ✅ 抽象对，**但覆盖面只到 remote 拓扑**（见 R2-1） |
| 队列 schema 幂等 + 锁 | `remote.py:173-184`（`pg_advisory_lock`，注释注明 `schema.sql` 非幂等） | ✅ 踩过坑才写得出来 |
| 连接池取消安全 | `remote.py:329-340`（`except BaseException`+先释放槽位+唤醒哨兵+再关连接）、`:353-355`（建连取消回滚槽位） | ✅ 配平逻辑正确 |
| 取消跨进程接线 | `executor.py:92-107`（`cancel_turn` 统一入口，remote 优先 `pg_notify`） | ✅ 接线干净 |
| 连接数收敛 O(节点) | `NotifyHub`(1 LISTEN) + `TurnFetchPool`(8) + 队列池 ≈ 13 | ✅ api 侧连接数不再是扩容变量 |

**基线**：`tests/unit/cluster` + `test_remote_executor.py` + `test_executor.py` = 46 passed；`make all` 全绿（lint 0 / mypy 0）。

---

## 2. 缺陷全量清单（合并去重）

HANDOFF 10 项与 assessment 5 项存在重叠，合并后共 **14 项**（去重 1 项：HANDOFF §8.2 ≡ assessment §3.A；R2-5 ≡ §3.B 但影响被评估加重）。

| ID | 级别 | 问题 | 位置（已核实） | 来源 |
|---|---|---|---|---|
| D1 | 🔴 | 自愈对"活着且心跳正常、但回合跑得久"的作业误重派 → 双跑 + 串行化失效 | `remote.py:1143`（心跳型，带 `seconds_since_heartbeat`）、`:1156-1160`（运行时型，**只传 `nb_seconds=2100`，不看心跳**）、`:1164` 直接 `retry_job` | HANDOFF §8.2 / assessment §3.A |
| D2 | 🔴 | 渠道运行时状态只在 leader 生效，**无跨节点传播**；增删改经 LB 落到非 leader 即不生效/删不掉 | `gateway.py:662-669`（非 leader `_safe_register_channel` 直接 return，只写 standby）；`reload_channels_from_db` 仅 3 调用点（`server.py:536/537`+`backup.py:202`）；`create/update/delete`（`gateway.py:340/375/399`）只操作本节点；gateway 目录 grep `pg_notify\|LISTEN\|NOTIFY` **零命中** | assessment §3.B（HANDOFF R2-5 仅提 UI 闪烁，**实际更严重**） |
| D3 | 🔴 | api 崩溃/滚动升级后，在途远端回合无法续接 → "RPO=0/零丢失"对 remote 模式不成立 | `remote.py:706`（`job_id=new_ulid()` 不落业务表）；`observe_chunk` 唯一调用点 `processor.py:279`（worker 不写轨迹）；`PgNotifyBus` 全 src **零实现**，`TrajectoryLiveBus` 是进程内字典（`live.py:9`） | assessment §3.C（HANDOFF 未覆盖） |
| D4 | 🔴→✅ | 阶段 0 形态（N 个 `octop run`+LB）无门控（已修：`OCTOP_LEADER=0` 副本为纯 HTTP 节点，显式 leader= 参数优先；test_local_leader_gate）：local 多副本每副本都内联跑全部后台单例（cron/IM/proactive/知识恢复 ×N+1 次） | `server.py:383`（`LeaderRunner` 只在 `execution_mode=="remote"` 内创建）、`:408`（`BackgroundGate(leader_runner, enabled=self._leader_enabled)`） | HANDOFF R2-1 |
| D5 | 🔴 | 控制面 `run_migrations` 无跨进程互斥；`--scale` 并发启动撞重复对象，`_ensure_published_experts_schema` 并发重建更危险 | `infra/db/migrate.py`（grep `advisory` 零命中；对比队列 schema 有锁 `remote.py:184`） | HANDOFF R2-2 |
| D6 | 🟠→✅ | 文档启动命令参数不存在（已修：文档按实际 CLI 重写 + test_documented_cli_flags 防漂移）| `docs/deployment-stage2.md:37,51`（`octop agent-worker --workspace`）→ 实际 CLI 只 `--concurrency`/`--debug`（`agent_worker.py:53-61`） | HANDOFF R2-3 / assessment §4#1 |
| D7 | 🟠→✅ | 文档"worker 启动预热"描述与实现未对齐（已修：prestart_agents 措辞，warmup 保留为可选注入点）；`warmup` 参数生产无调用方（仅 `scripts/stage2/worker.py` 传） | `remote.py:956,970,981,987`（warmup 定义/使用）；实际靠 `prestart_agents=True` 规避 DDL 死锁（`server.py:524`） | HANDOFF R2-4 / assessment §4#2 |
| D8 | 🟠→✅ | worker `prestart_agents=True` → 启动即全量加载 agent（已修：文档明确内存取舍为有意决策），与"内存 ∝ 活跃会话数"设计目标相反 | `server.py:524`（`remote_executor is None or not self._leader_enabled`）→ `manager.py:501-504` | assessment §4#3 |
| D9 | 🟡 | LB 用变量 + resolver，无 `upstream` 块 → 配不了 `max_fails`/`proxy_next_upstream`；挂一个 api → 1/N 请求 502；`/api/health` 仅 liveness 无 readiness | `docker/cluster-lb.conf`（全文 33 行，`set $octop_api`+`proxy_pass $变量`，无 upstream 块）；`health.py`（`ok` 恒 True） | HANDOFF R2-6 / assessment §4#4 |
| D10 | 🟡→✅ | 队列串行文档表述与实现矛盾（已修：production-ha.md 两处均纠偏为 `lock` 串行 / `queueing_lock` 禁用） | `production-ha.md:50` | assessment §6（已纠偏，文档待同步） |
| D11 | 🔵 | 分层违规：`infra/` 反向 import `api/`，违反 AGENTS.md §5 硬禁令 | `remote.py:103`（`from octop.api.routers.chat.sse import json_chunk_default`） | HANDOFF R2-7 / assessment §4#9 |
| D12 | 🔵→✅ | 5 个压测/证据脚本硬编码本机解释器路径（已修：`scripts/support/pg_python.py` 共享解析，OCTOP_PG_PYTHON 环境变量优先；result.json 路径脱敏） | `scripts/{poc1,poc2,poc3,stage2,stage3}/run_*.py` + 2 个 `result.json` | HANDOFF R2-8 / assessment §4#8 |
| D13 | 🟠 | `publisher.open()` 在 try 之外 → open 失败不发错误哨兵，读者等满 1800s 看门狗 | `remote.py:872-873`（open）、`:875`（try 始）、`:892-904`（except 只兜 try 内） | assessment §3.D（HANDOFF 未覆盖） |
| D14 | 🔵 | `retry_stalled_jobs_once` 无逐作业容错；并发扫描撞 `retry_job` RAISE → 本轮剩余作业全跳 + 日志噪音 | `remote.py:1148-1165`（逐个 `await job_manager.retry_job(job)`，无 per-job try/except） | assessment §3.E（HANDOFF 未覆盖） |
| D15 | 🔴 | worker 假活（存活+心跳新鲜+回合卡住）→ 线程永久卡死：心跳自愈扫不到、api 看门狗不释放锁 | 09-26 整体评审 §3 实测（S3 探针）。正解 `job_timeout` 经核实 procrastinate 3.10 不存在，改为自建 `_execute_turn` 运行时上限 | cluster-holistic-review-2026-09-26 |

---

## 3. 优先级与改法（综合排序）

排序原则：**"会不会真的伤到用户" > 改动量**。每项含改法方向与验证标准。

### 批次 1：堵核心保证的洞（🔴，改动小）

#### D1 · 自愈误重派收窄
- **改法**：运行时兜底只在 worker 确认真死时重派——即 `long_running` 集合先与 `heartbeat_stalled`（worker 行已被 prune）取交集，或对 `long_running` 中的作业额外校验 `worker_id IS NULL OR worker 已停跳`；或将阈值抬到明显高于正常回合上限（需与 api 侧 1800s 看门狗关系重新推演）。
- **验证**：回归测试 `test_runtime_retry_only_when_worker_dead`（构造 doing>2100s 但 worker 心跳正常的作业，断言不重派）+ assessment §3.A 探针脚本复跑。
- **量级**：小。

#### D13 · publisher.open 纳入 try
- **改法**：把 `await publisher.open()` 移入 try 块，或在 open 失败时直接发错误哨兵（`publisher.fail`）。
- **验证**：`test_open_failure_emits_sentinel`（mock open 抛异常，断言读者收到错误哨兵而非等 1800s）。
- **量级**：小。

#### D14 · 逐作业容错
- **改法**：`retry_stalled_jobs_once` 的 `for job in long_running` 循环每个作业包 `try/except`，单作业失败仅记日志不中断本轮。
- **验证**：`test_retry_continues_after_one_job_raises`。
- **量级**：小。

### 批次 2：渠道状态传播（🔴，中改动，用户最易投诉）

#### D2 · 渠道变更跨节点传播
- **改法（二选一，推荐 A）**：
  - A. 渠道写操作后发 PG `NOTIFY`，leader `LISTEN` 后 `reload_channels_from_db`——对齐 `remote.py` 已有的 `_CancelNotifier` 模式，机制一致；
  - B. 渠道写操作收敛到 leader（follower 经 leader 转发）——改动面大，但语义最简单。
- **验证**：`test_channel_change_propagates_to_leader`（双节点 mock，follower 增渠道，断言 leader 收到 NOTIFY 后 reload）。
- **量级**：中。
- **注意**：HANDOFF R2-5 原描述"UI 闪烁"严重低估，实际是"功能不生效/删不掉"，优先级应上调。

### 批次 3：补覆盖面（🔴，依赖部署形态决策）

#### D4 · 阶段 0 多副本门控
- **改法**：给 local 多副本加显式门控——`OCTOP_LEADER=1` 环境变量，或让 `BackgroundGate` 感知多副本配置（local 模式下也走 advisory lock 选主，而非默认 enabled=True）。
- **验证**：`test_local_multireplica_runs_singletons_once`（两实例 local 模式，断言后台单例只跑一份）。
- **量级**：小。
- **依赖**：岔路一（部署形态）。

#### D5 · 迁移跨进程锁
- **改法**：对标 `remote.py:184` 的 `ensure_queue_schema`，在 `run_migrations` 入口加 `pg_advisory_lock` 串行化（迁移本就短，锁开销可忽略）。
- **验证**：`test_migrations_take_advisory_lock`（断言迁移入口持 advisory 锁）。
- **量级**：小。

### 批次 4：api 崩溃续接（🔴，架构取舍）

#### D3 · 在途回合续接
- **改法（二选一，需拍板）**：
  - A. **实现续接**：落库 `thread → 当前 job_id` 映射表（worker 认领时写、完成时清）；实现 `PgNotifyBus` 让 SSE 走"DB 回放 + 跨节点 LISTEN 唤醒"（设计文档 §4.3/§6 原本就这么规划）。兑现"RPO=0/滚动升级不中断"承诺；
  - B. **降级承诺**：把 `production-ha.md` §0/§2.1/§2.6 的"零丢失/滚动升级不中断"改为"已落库部分可回放"，代码不动。诚实、省工作量，但放弃一个卖点。
- **验证**：A 方案需 `test_api_crash_continues_turn`（双节点，api#1 崩后 api#2 续接同一 job 流）；B 方案需文档审查清单。
- **量级**：A=中；B=文档。
- **依赖**：岔路二。

### 批次 5：LB 健康检查（🟡）

#### D9 · LB 健康检查落地
- **改法（二选一）**：
  - A. 改回 `upstream` 块 + `max_fails`/`proxy_next_upstream`，牺牲 `--scale` 免重载（扩缩容需 reload nginx）；
  - B. 上云 LB（ALB/CLB）替代 nginx 做 health check；
  - 同时给 `/api/health` 补 readiness 语义（含迁移版本匹配、DB 可达）。
- **验证**：`test_health_endpoint_readiness`；实测挂一个 api 容器不再 1/N 502。
- **量级**：小。

### 批次 6：收尾与文档（🟠🟡🔵）

| ID | 改法 | 验证 |
|---|---|---|
| D6 | 文档改按实际 CLI 参数写（workspace 靠环境变量） | `test_documented_cli_flags_exist`（挡住文档与 CLI 漂移） |
| D7 | 同步 `deployment-stage2/3.md` 措辞，说明 `prestart_agents` 取代 warmup | 文档审查 |
| D8 | 文档明确"worker 内存 ∝ agent 总数 × worker 数"是当前取舍（换 DDL 安全），或实现真懒加载 | 文档/或代码 |
| D10 | 文档纠偏：`lock`=串行（保留）、`queueing_lock`=去重（会丢消息，禁用） | 文档审查 |
| D11 | `json_chunk_default` 下沉到 `infra/utils/`，api 侧反向引用 | `make all` + import 检查 |
| D12 | 脚本改 `sys.executable` 或环境变量；`result.json` 路径脱敏 | 跨机复现 |

**文档三份交叉对照**：`production-ha.md` / `deployment-stage3.md` / `microservice-scaling-1000.md`，改完一遍，别再引入 P3-9 式矛盾。

---

## 4. 需要拍板的两个岔路

> **2026-09-26 已拍板岔路一**：部署形态以 **stage3（remote，api+worker 分离）为准**，阶段 0 不再作为推荐形态。
> 产品目标：**集群使用体验与单机体验一致**。据此：
> - D4（阶段 0 多副本门控）降级为低优先级，排到最后与批次 6 合并处理；
> - D3 倾向方案 A（实现续接）——"体验一致"要求 api 崩溃/升级不中断在途会话，文档降级与目标相悖。待开工批次 4 时最终确认。

### 岔路一·部署形态【已拍板：stage3 remote】
- **remote 模式**（stage3，api+worker 分离）：D4/D5 可排在 D2 之后；
- **阶段 0**（N 个 `octop run`+LB，文档标"推荐✅"）：D4 必须提前到 D1 之后，否则后台单例被 N 副本各跑一遍。

### 岔路二·D3 续接 vs 降级
- **实现续接**：兑现承诺，但需落库映射 + `PgNotifyBus`，中等工程量；
- **降级文档**：诚实但放弃卖点，零代码改动。

---

## 5. 修复纪律（沿用 HANDOFF §4）

1. **每项配针对性回归测试**（几行断言挡整类回归，对齐第三轮风格如 `test_worker_role_starts_no_background_singletons`）。
2. **新逻辑收敛进 `octop.infra.cluster` 包**，社区共享文件（`server.py`/`ws.py`/`gateway.py`）足迹最小，为与社区代码合并减冲突。
3. **文档改完过三份交叉对照**：`production-ha.md`/`deployment-stage3.md`/`microservice-scaling-1000.md`。
4. **架构实质变更后在 `docs/architecture-evolution.md` 追加 ASCII 图**（★ 标变化点，注版本代号/日期）。

---

## 6. 验证命令

```bash
cd /Users/timevaeless/Documents/Work/Octop-cluster

# cluster 相关单测（必须去 PYTHONPATH，WorkBuddy shim 会拦 mkdir/rmtree 炸 xdist）
env -u PYTHONPATH ~/.local/bin/uv run --frozen pytest \
  tests/unit/cluster tests/unit/agents/test_remote_executor.py tests/unit/agents/test_executor.py -q

# 全量质量门（lint + mypy + pytest）
env -u PYTHONPATH ~/.local/bin/uv run --frozen make all

# D1 自愈探针（真 PG + 真 procrastinate）
docker exec octop-postgres psql -U octop_admin -d postgres \
  -c "DROP DATABASE IF EXISTS octop_probe_rv" -c "CREATE DATABASE octop_probe_rv"
TMPDIR=.workbuddy-ai/tmp .venv/bin/python .workbuddy-ai/tmp/probe/probe_stalled_double_run.py
```

**基线**：当前 46 passed；每修一项后应保持 `make all` 全绿。

---

## 7. 建议执行批次与里程碑

| 批次 | 项 | 里程碑（可验证） |
|---|---|---|
| 1 | D1 + D13 + D14 | 长回合不再双跑；open 失败不再让读者等 30 分钟 ✅（efc958e1） |
| 2 | D2 | 渠道增删改跨节点即时生效 |
| 4 | D5 | 迁移不撞（D4 随岔路一拍板降级，并入批次 6）✅（a03d6882 互斥 + 本轮 round2 D5：try_lock 轮询+等待日志+600s 超时 loud fail） |
| 5 | D3 | 在途回合续接（方案 A：映射表+attach 续播端点）✅（c4be23b5）+ WS subscribe 跨节点续流（前端零改动闭环）✅（574bf830）+ 孤儿处理：attach 前查 job 状态，终态（failed/cancelled/aborted/行不存在）快速失败 ✅（a6e7ec99）；投影级续接待后续 |
| 5.5 | D15 | 回合运行时上限（假活卡死判死放锁，不双跑）✅（49cff993） |
| 6 | D9 | LB 能摘除故障 api，不再 1/N 502 ✅（17476b64：upstream+resolve 保留免重载+max_fails 摘除；readiness=`/api/health/ready`） |
| 7 | D6-D8,D10-D12 + D4 | 文档与实现零矛盾；脚本可跨机复现 ✅（D6 防漂移测试 + D7/D8 措辞 + D10 勾注 + D12 pg_python 解析 + D4 OCTOP_LEADER 门控）（D11 ✅：`json_chunk_default` 下沉 `infra/utils/json_serialize.py`，api 侧 re-export，`remote.py` 反向 import 已消除） |

**第一批（D1/D13/D14）即可立即开工**——三项都在 `remote.py` 同一文件、改动小、互不冲突，可一并提交并配 3 条回归测试。

## 8. 插拔契约（2026-09-26 起，长期有效）

目标：cluster 改造收敛为**可整体移除的模块**——把 `octop/infra/cluster/`、`octop/api/cluster/` 两个包从源码树删掉（或打包时排除），社区单机版（SQLite + `InProcessExecutor`）功能零影响。

### 8.1 物理边界

| 位置 | 内容 |
|---|---|
| `octop/infra/cluster/` | `remote.py`（队列执行/续接/D15 运行时上限，自 `infra/agents/remote.py` 迁入）、`leader.py`（选主/BackgroundGate）、`channel_bus.py`（D2 通知） |
| `octop/api/cluster/` | `routes.py`（D3 SSE attach 端点，`execution_mode=remote` 时才挂载）、`relay.py`（WS 跨节点续流助手） |

**留在核心的 cluster 相关代码**（有意决策，非遗漏）：
- `migrate.py` D5 迁移锁：PG 鲁棒性改进，社区 PG 用户同样受益（SQLite 默认后端完全不触发）；
- `health.py` `/ready`：通用 readiness 语义；
- `gateway.py` 的 `channel_notify`/`executor` 构造参数：默认 None，社区路径零行为差异；
- `config.py` 的 `execution_mode`/`queue_dsn`：默认 `local`/空。

### 8.2 接线规则（核心代码引用 cluster 的唯一合法方式）

1. **只允许函数体内懒加载**（模块顶层 import 即违规）；
2. **必须先守卫再 import**：`execution_mode == "remote"` / remote_executor 存在 / `_leader_enabled`，社区路径永不执行到 import 语句；
3. **必须有 ImportError 兜底**：`server.py` 的 BackgroundGate 用 `_StandaloneBackgroundGate`（核心内联版）兜底，cluster 包缺席也能 boot。

### 8.3 契约测试（回归守卫）

`tests/unit/cluster/test_pluggability.py`：
- `test_no_top_level_cluster_imports_outside_the_cluster_packages`：AST 静态扫描全 `src/octop`，cluster 包之外出现**模块顶层** cluster import 即 fail（同时钉死历史路径 `octop.infra.agents.remote` 不得回归）；
- `test_community_boots_with_cluster_modules_blocked`：毒化 `sys.modules` + meta_path 拦截模拟"物理删除"，完整 boot 社区服务并 build_app，断言社区路由面完整（openapi 377+ paths）、无 cluster 路由泄漏、SQLite 无 `octop_turn%` 表。

**改动接线点时必须同步跑这两个测试。** 快速拔除验证：`rm -rf src/octop/infra/cluster src/octop/api/cluster` 后 `make test`（排除 cluster 目录的测试）应全绿。
