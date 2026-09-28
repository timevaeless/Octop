# Octop Cluster 改造 · 整体提交梳理 + 代码评审（2026-09-26）

> 范围：分支 `feat/cluster-stage3`，分叉点 `c04aacc6`，截至 `55d09315`（09-26 07:45）
> 方法：读全部提交 + 读核心代码 + 真 PG 探针复跑 + 单测（62 passed）
> 一句话结论：**架构骨架是对的、越做越干净；但"假活线程永久卡死"这个洞一直没堵（原评审给的正解 `job_timeout` 从未落地），且文档/代码/注释三处仍在互相打架。**

---

## 0. 一句话结论（先给效果）

| 你现在的集群 | 状态 |
|---|---|
| api / worker / 后台单例 三角色 | ✅ **已经生效**，边界干净 |
| 选主（进程死即释放锁） | ✅ **已经生效**，比 TTL 租约正确 |
| 渠道改动跨节点即时生效（D2） | ✅ **已经生效**，实测通过 |
| 迁移并发不撞（D5） | ✅ **已经生效**，实测通过 |
| 长回合不再双跑（D1 前半） | ✅ **已经生效** |
| **回合卡住（worker 假活）能自愈** | 🔴 **有洞没堵** —— 线程永久卡死，无兜底 |
| **文档/注释与代码一致** | 🔴 **有洞没堵** —— 4 处文档 + 2 处代码注释仍在说旧行为 |

---

## 1. 提交记录梳理（这条分支到底做了什么）

从分叉点 `c04aacc6` 起，cluster 相关工作共 **11 个提交**（另有 4 个 multi-tenant / 2 个 connector / 3 个 dashboard 提交作为底座，与 cluster 无关）。

| # | 提交 | 日期 | 做了什么 | 一句话评价 |
|---|---|---|---|---|
| 1 | `427b1ff2` | 09-25 09:33 | **执行器抽象**：`AgentExecutor` 协议 + `InProcessExecutor`；`octop agent-worker` CLI；POC1-3 脚本 + 6 份设计文档 | 抽象切口选得准——5 个调用点靠构造注入换执行器，调用点零改动 |
| 2 | `1a2c972a` | 09-25 11:18 | **stage3**：`RemoteExecutor`/`AgentWorker`（PG 队列）；`cluster/leader.py` 选主；集群 compose + LB conf；PG 压力收敛 | 主体成型；选主机制选得对 |
| 3 | `14be3500` | 09-25 11:55 | 评审修复：过渡串行化（`_transition_lock`）+ keepalive + 接线收敛进 cluster 包 | 把启动/停止竞态堵住了 |
| 4 | `61ed6302` | 09-25 14:44 | 评审 P0/P1 全量：锁自愈 + 角色隔离 + 取消跨进程 + 池取消安全 | 修得扎实，7 条回归测试 |
| 5 | `7e19d1ca` | 09-25 14:05 | 架构演进图集（ASCII），约定每次变更追加一图 | 好习惯——拓扑变更可追溯 |
| 6 | `e603705f` | 09-25 20:24 | 归档外部评审报告 + 方案讲解页 | — |
| 7 | `efc958e1` | 09-25 22:19 | **第一批缺陷修复**：D1 自愈收窄 + D13 open 哨兵 + D14 重派容错 | D13/D14 好；D1 修了一半（见 §3） |
| 8 | `7da1cc52` | 09-26 03:38 | ruff format 漂移修复（21 文件，独立提交） | 干净——格式化与功能分离 |
| 9 | `f9eae9e2` | 09-26 03:39 | 归档交接清单/修复评审/演进图集；**拍板岔路一** | 决策记录清楚 |
| 10 | `df3221bf` | 09-26 03:39 | **D2 渠道变更跨节点传播**（写操作 NOTIFY + leader LISTEN 重载） | 机制对；一处语义隐晦（见 §4） |
| 11 | `a03d6882` | 09-26 07:45 | **D5 控制面迁移跨进程互斥**（PG 入口 advisory lock） | 对标既有模式，正确；一处无超时（见 §4） |

**演进脉络**：`抽象切口` → `机制成型` → `评审三连修` → `缺陷收口（D1/D13/D14 → D2 → D5）`。方向始终是"新逻辑收敛进 `infra/cluster` 包、社区共享文件足迹最小"，这点做得很好。

---

## 2. 架构评审：做得对的部分（先说好的）

| 机制 | 评价 |
|---|---|
| **三角色拓扑** | ✅ api 不加载 agent（内存 O(1)）/ worker 纯执行（`leader=False`，`BackgroundGate(enabled=False)` 跳过全部后台单例）/ 后台单例仅 leader 跑。职责边界用**显式 `enabled` 标志**表达（而非靠 `runner is None` 推断），堵住了"worker 被误判成 standalone 而跑满后台"的坑 |
| **会话级 advisory lock 选主** | ✅ 进程死=连接断=锁服务端自动释放，**无 TTL、无时钟依赖、无脑裂窗口**。比 TTL 租约正确。keepalive（idle=30/interval=10/count=3）+ `SELECT 1` 带 `wait_for` 超时把半开连接接管压到秒级 |
| **锁 key 分段** | ✅ 四个独立 key：`918273645` 队列 schema / `918273646` worker warmup / `918273647` 选主 / `918273648` 迁移。互不冲突，还有测试断言其互异——细心 |
| **连接数 O(节点)** | ✅ `NotifyHub`（1 LISTEN）+ `TurnFetchPool`（默认 8）+ 队列池 + `ChannelChangeWatcher`（1）。api 侧连接数不再随 agent 数放大 |
| **取消跨进程** | ✅ `cancel_turn` 统一入口（remote 优先 `pg_notify`，local 走 registry）+ worker 侧 LISTEN 重连循环。接线干净 |
| **D2 传播语义** | ✅ DB 行是唯一真相 + reload 幂等 → NOTIFY 可 best-effort、失败不连累写操作。设计对 |
| **D5 迁移锁** | ✅ 对标 `ensure_queue_schema` 的既有模式，`try/finally` 确保 unlock |
| **过渡串行化** | ✅ `_transition_lock` + `_handles_running` 双闸，保证 start/stop 恰好一次；有对应单测 |
| **分层纪律** | ✅ 除一处历史遗留（见 §4-D11），cluster 逻辑都在 `infra/cluster` 包内 |

**单测基线**：`tests/unit/cluster` + `test_migrate_lock.py` + `test_remote_executor.py` + `test_executor.py` = **62 passed**（本机实测）。

---

## 3. 🔴 P1 未堵：worker 假活 → 线程永久卡死（上轮已提，本轮仍在）

### 结论
`efc958e1` 把运行时兜底（`nb_seconds=2100`）从"重派"改成"只监控"，声称"worker 真死/**卡死**由 heartbeat 覆盖"。**这半句不成立**——心跳检测按"心跳停跳"判定，扫不到"心跳正常但回合卡住"。

### 实测（三场景探针，清空队列后复跑，09-26）
```
[S1] 孤儿(worker_id IS NULL)  -> (1,'todo')   ← 恢复 ✅
[S2] 真死(心跳停跳 180s)      -> (2,'todo')   ← 恢复 ✅
[S3] 假活(存活+心跳新鲜+卡住) -> (3,'doing')  ← 不重派
连续 6 轮扫描: retried=0  job3 恒为 (3,'doing',3)
heartbeat 检测器命中: []          ← ★ 扫不到 S3
同线程后续消息 3 次认领 -> None   ← ★ 永久卡死
```

### 为什么仍是真实风险
- **触发场景真实**：LLM 上游 hang（TCP 连上不再吐 token）、工具调用永不返回、DB 查询挂住。
- **api 侧救不了**：1800s 看门狗只 `raise RemoteTurnError`（`remote.py:478-485`），**不发 cancel**；worker 侧**无回合超时**。
- **无自愈**：`retry_stalled_jobs_once` 只认心跳停跳，S3 心跳正常 → 永不重派。

### 根因（不只是代码，计划本身也漏了）
原评审 `cluster-review-2026-09-25.md:95` 给的 S3 正解是 **`job_timeout`**；但 `cluster-implementation-plan` 的 D1–D14 清单里**根本没有 `job_timeout` 这一项**。于是：计划没排它 → 修复没做它 → 洞还在。

**已核实**：全 `src/` grep `job_timeout` **零引用**。

### 代码内注释也在撒谎（同源）
- `remote.py:1132-1141`（`_stalled_loop` docstring）：仍写 *"a job that hangs forever would keep the per-thread lock forever ... This loop retries stalled jobs back to `todo`"* —— 代码已不 retry hang。
- `remote.py:91-94`（`TURN_JOB_MAX_RUNTIME_SECONDS` 注释）：仍在论证 *"retrying (freeing the thread lock) is safe"* —— 代码不 retry 了。

### 建议
新增 **D15：补 `job_timeout`**（procrastinate 自带）。给回合任务设一个明显高于正常回合上限的超时，让 procrastinate 自己把卡死作业判失败 → 释放锁。既覆盖 S3，又不引入双跑（超时即判死，不存在"原 worker 还在跑"）。这是三条路（双跑 / 永久卡死 / 超时判死）里唯一可接受的。

---

## 4. 代码评审发现（本轮新）

### P2 · 文档 4 处与代码相反（上轮已提，未修）
| 位置 | 原文 | 现状 |
|---|---|---|
| `docs/production-ha.md:53` | `超 2100s 的 doing 作业兜底（覆盖"worker 假活"），retry_job() 重派释放锁` | ❌ 代码已删重派 |
| `docs/microservice-scaling-1000.md:173` | `心跳 60s + 运行时 2100s 双检测` | ❌ 运行时那半只监控 |
| `docs/deployment-minio-pgvector.md:136` | `必配 retry_stalled_jobs 周期任务（1min）` | ❌ 已内置于 `_stalled_loop`，非"必配周期任务" |
| `docs/production-ha.md:150` | `队列锁规范：只用 queueing_lock，禁用 lock` | ❌ **与实现相反**——代码用 `lock`（正确，`queueing_lock` 是去重会丢消息）。这正是计划 D10 要纠偏的那条 |

> 顺带：`production-ha.md:133-138` 的"阶段 3 落地更新"块**自身就矛盾**——`:137` 称 `retry_stalled_jobs` 周期任务"**仍未接入，生产启用前必须补上**"，而同一文件的 `:53` 已把它描述成"已实现、retry_job 释放锁"；`:144` 又列为"代码（小）待办"。三处口径不一。

### P3 · D11 分层违规仍在
`src/octop/infra/agents/remote.py:103`：
```python
from octop.api.routers.chat.sse import json_chunk_default  # noqa: PLC0415
```
`infra/` 反向 import `api/`，违反 `AGENTS.md §5` 硬禁令（全 `infra/` 仅此一处）。建议把 `json_chunk_default` 下沉到 `infra/utils/`。

### P3 · worker 节点构造了语义错位的 `RemoteExecutor`
`docker/docker-compose.cluster.yml` 里 worker 也用 `OCTOP_EXEC_MODE=remote`。于是 `server.py:386-395` 在 **worker 节点也创建 `RemoteExecutor`**，并在 `:471` 作为 `gateway.executor` 注入。

- worker 的 gateway 虽不接客（不跑 HTTP），当前靠"worker 不跑后台单例"兜住，**无实际 bug**；
- 但这是概念错位：worker 不该具备"把回合丢回队列"的能力——若将来任何后台路径在 worker 上触发 gateway，会造成**回合二次入队**。
- 建议：`execution_mode=="remote"` 时仅在 `_leader_enabled` 节点创建 `RemoteExecutor`（worker 走 `InProcessExecutor`）。

### P3 · D5 的 `pg_advisory_lock` 阻塞式、无超时
`migrate.py:1962` 用 `pg_advisory_lock`（阻塞版）。若持锁节点在迁移中途挂住（等待某个锁 / 慢 DDL），其余节点 **boot 无限期阻塞**，且无日志、无告警。
建议：改 `pg_try_advisory_lock` + 有限重试 + 超时告警（或至少给阻塞加一条 INFO 日志说明"等待其他节点迁移"）。

### P4 · D2 watcher 重连失败后未清空 `self._conn`
`channel_bus.py:135-144`：reconnect 失败时 `self._conn` 仍指向**旧已关闭连接**。下一轮 `conn = self._conn` → `conn.notifies()` 立即抛错 → 再走一次重连。能自愈，但多绕一圈、日志噪音。
建议：reconnect 失败时 `self._conn = None`（`_loop` 顶部已有 `if conn is None: return` 分支，但那会直接退出——需一并调整，否则变成"退出而不重试"）。

> ⚠️ 注意：`_loop` 顶部 `if conn is None: return` 意味着**若把 `self._conn` 置 None，watcher 会永久退出**。所以当前"保留旧连接"其实是有意为之的"靠异常驱动重试"。修的时候要把这个分支也改掉（改成 sleep+continue），否则会把"噪音"换成"永久失效"。**这里保留现状也可接受，但值得加一行注释说明这是有意的。**

### P4 · `reload_channels_from_db` 同时充当 IM 通道的 start 与 stop handle
`server.py:560-564`：
```python
await gate.route(
    "gateway-im-channels",
    gateway.reload_channels_from_db,   # start
    gateway.reload_channels_from_db,   # stop
)
```
能工作是靠一个隐晦的性质：**失去领导权时 `im_active()` 已变 False，reload 会先 unregister 全部通道、再因 `im_active` 为假而跳过重新注册** → 净效果是"停掉 poller"。

- 语义隐晦、易碎：谁要是给 `reload_channels_from_db` 加个 early-return（比如"无变化就跳过"），就会 silently **停不掉 poller**；
- 建议：给 Gateway 一个显式 `stop_channels()`（或 `unregister_all()`）作为 stop handle，把"停"的意图写进名字。

---

## 5. D1–D14 状态总表（截至本轮）

| ID | 级别 | 内容 | 状态 |
|---|---|---|---|
| D1 | 🔴 | 自愈误重派收窄 | 🟡 **半修**：双跑消除 ✅，但 S3 假活无兜底（§3） |
| D2 | 🔴 | 渠道变更跨节点传播 | ✅ **已修**（`df3221bf`，9 测试） |
| D3 | 🔴 | api 崩溃续接 | ⬜ 未修（已倾向方案 A） |
| D4 | 🔴 | 阶段 0 多副本门控 | ⬜ 已按岔路一降级 |
| D5 | 🔴 | 迁移跨进程锁 | ✅ **已修**（`a03d6882`，4 测试） |
| D6 | 🟠 | 文档 CLI 参数不存在 | ⬜ 未修 |
| D7 | 🟠 | worker 预热描述未对齐 | ⬜ 未修 |
| D8 | 🟠 | worker 全量 prestart 与设计相反 | ⬜ 未修 |
| D9 | 🟡 | LB 健康检查 | ⬜ 未修 |
| D10 | 🟡 | 队列串行文档矛盾 | ⬜ 未修 |
| D11 | 🔵 | 分层违规 infra→api | ⬜ 未修（§4） |
| D12 | 🔵 | 脚本硬编码路径 | ⬜ 未修 |
| D13 | 🟠 | open 失败发哨兵 | ✅ **已修**（`efc958e1`） |
| D14 | 🔵 | 重派逐作业容错 | ✅ **已修**（`efc958e1`） |
| **D15** | 🔴 | **补 `job_timeout`（S3 正解）** | 🆕 **建议新增** |

进度：**5 项已修 / 1 项半修 / 9 项未动 / 1 项建议新增**。

---

## 6. 建议顺序

1. **D15（新增，最高优先）** —— 补 `job_timeout`，堵住"假活永久卡死"。这是唯一还在的 🔴 可用性洞。
2. **文档三处对齐（§4 P2）+ 代码内两处注释对齐（§3）** —— 低成本，消除"文档骗人"。
3. **D3 续接** —— 已倾向方案 A，兑现"集群体验=单机体验"。
4. **D9 LB 健康检查** —— 影响可用性（挂一个 api → 1/N 502）。
5. **D11 分层 + worker 无用 RemoteExecutor（§4 P3）** —— 低成本清理。
6. 其余 D6–D8/D10/D12 文档与脚本收尾。

---

## 7. 诚实边界

- 本轮 D2/D5 的结论来自**读代码 + 单测**；D2 的"跨节点真传播"未做双进程真 PG 端到端实测（单测用 mock 的 `on_notify`），D5 未做真并发 `--scale` 实测。**建议补两条真环境探针**（双 api 进程 + 真 PG，验证渠道增删改在非 leader 节点落库后 leader 真重载；双进程同时 `run_migrations` 验证串行）。
- S3 的 P1 判定前提是"正常回合不会卡住"。若生产 LLM 上游/工具偶发 hang 频繁，应升为 **P0**。
- `job_timeout` 的可用形态需按当前 procrastinate 版本确认（3.10）。

---

## 8. 复现命令

```bash
cd /Users/timevaeless/Documents/Work/Octop-cluster

# 单测基线（62 passed）
mkdir -p .workbuddy-ai/tmp/pytest
TMPDIR=.workbuddy-ai/tmp .venv/bin/python -m pytest \
  tests/unit/cluster tests/unit/db/test_migrate_lock.py \
  tests/unit/agents/test_remote_executor.py tests/unit/agents/test_executor.py \
  -q -p no:cacheprovider --basetemp=.workbuddy-ai/tmp/pytest

# S3 假活探针（证伪"心跳覆盖卡死"）
docker exec octop-postgres psql -U octop_admin -d octop_probe_rv \
  -c "TRUNCATE procrastinate_jobs, procrastinate_events, procrastinate_workers RESTART IDENTITY CASCADE;"
TMPDIR=.workbuddy-ai/tmp .venv/bin/python .workbuddy-ai/tmp/probe/r1_probe_stall_selfheal.py
```
