# Octop 集群（stage 2/3）改造方案独立评估

> 评估日期：2026-09-25 ｜ 被评估分支：`feat/cluster-stage3`（HEAD `e603705f`）
> 涉及提交：`427b1ff2`（执行器抽象 + RemoteExecutor）、`1a2c972a`（选主 + 压测 + 编排）、
> `14be3500` / `61ed6302`（两轮评审修复）
> 方法：代码逐行走读（带 `文件:行号`）+ 真 PG 探针实测 + 依赖源码（procrastinate）语义核对 + 单测实跑
> 交叉验证：`docs/cluster-review-2026-09-25.md`（已归档的三轮评审报告）
>
> **本轮没有改动任何源码。**

---

## 0. 一句话结论

**方向对、关键选型对、机制级也有真证据；但"生产级"下得偏早。**

缺口不在核心机制，而在两处：

1. **覆盖面**——`BackgroundGate` 的第三态（"我是集群节点但不跑后台单例"）只覆盖了 remote 拓扑，
   你们自己文档标为「推荐 ✅」的**阶段 0 形态（N 个 `octop run` + LB）仍然没有门控**；
   控制面迁移也没有跨进程锁。这两条是"照你们自己的部署文档做就会踩"。
2. **故障恢复的闭环**——自愈逻辑对"活着但回合跑得久"的作业会误重派（破坏串行化保证）；
   api 节点在生成中途崩溃后，在途回合**无法被新节点续接**，"轨迹 RPO=0 / 零丢失"对 remote 模式不成立。

| 状态 | 内容 |
|---|---|
| ✅ 已生效（我逐行/实测确认） | 选主机制（会话级 advisory lock）、过渡串行化、连接数收敛、连接池取消安全、取消跨进程接线、worker 角色隔离、队列 schema 幂等锁 |
| ⚠️ 做好了但没接线 / 与设计目标相反 | `warmup` 参数生产无调用方；worker 改为"启动即全量加载 agent"（与"内存 ∝ 活跃会话数"相反）；`job_id` 不落库（无法续接） |
| 🔴 有洞没堵 | 自愈误重派 → 双跑 + 串行化失效；渠道运行时状态不跨节点传播；阶段 0 多副本无门控；迁移无跨进程锁；LB 配不了健康检查 |

---

## 1. 改造思路（先理解它在做什么）

一句话：**把"一个进程包办一切"拆成三类角色，用 PostgreSQL 同时充当队列、回传通道和仲裁者。**

```
                      ┌──────── LB (nginx) ────────┐
                      │  轮询；SSE 关缓冲+长超时    │   ← 注意：无 upstream 块（见 §5）
                      └──────────┬─────────────────┘
              ┌──────────────────┼──────────────────┐
        ┌─────▼─────┐      ┌─────▼─────┐            │
        │  octop    │      │  octop    │  api ×N    │  HTTP/SSE/WS
        │  run      │      │  run      │            │  不预启动 agent（内存 O(1)）
        └─────┬─────┘      └─────┬─────┘            │  ★ 选主：抢到锁的跑后台单例
              │  advisory lock   │                  │
              │  918273647       │                  │
              ├──────────┬───────┘                  │
              │          │                          │
              │    ┌─────▼──────────────────────┐   │
              │    │  后台单例（恰好一份）        │   │  cron / IM 轮询 / proactive /
              │    │  cron · IM · proactive ·    │   │  知识恢复
              │    │  知识恢复                    │   │
              │    └────────────────────────────┘   │
              │                                      │
              │   回合任务（procrastinate 队列）      │
              │   payload={kind,job_id,agent_id,…}   │
              │   lock=octop-turn:{agent}:{thread}   │  ★ 同线程串行
              │                                      │
        ┌─────▼──────────────────────┐              │
        │  octop agent-worker ×N      │              │  leader=False，永不选主
        │  消费任务 → 本地执行 →       │              │  唯一执行回合的角色
        │  写 octop_turn_events       │              │
        │  + pg_notify                │              │
        └─────┬──────────────────────┘              │
              │                                      │
   ┌──────────▼──────────────────────────────────────▼───────────┐
   │ PostgreSQL：业务库 + 队列 + turn 事件表 + NOTIFY + advisory lock │
   │ 回传路径：worker 写事件表 → NOTIFY → api 侧 NotifyHub(1 LISTEN) │
   │           → TurnFetchPool(8) 增量 SELECT → SSE 推给浏览器        │
   └─────────────────────────────────────────────────────────────┘
```

四个关键设计决策（我的评价）：

| 决策 | 做法 | 评价 |
|---|---|---|
| 选主用什么 | 会话级 `pg_try_advisory_lock`（持在专用连接上），**不用 TTL 租约** | ✅ **正确**。进程死 = 连接断 = 锁由 DB 立即释放；无时钟偏移、无脑裂窗口。用 DB 做唯一仲裁者比租约简单且更强 |
| 队列用什么 | procrastinate（PG 原生，LISTEN/NOTIFY + SKIP LOCKED） | ✅ **合理**。契合"PG 已是硬前提、最少依赖"；回合级下发量比队列吞吐瓶颈低两个数量级 |
| 流怎么回传 | 事件表 + 50ms 批量 `pg_notify`，**不引 Redis** | ✅ **合理**。PoC 2 实测 p95 ≤51ms @100tok/s，50ms 批量天然控写放大 |
| 同线程怎么串行 | procrastinate 的 `lock` 字段（`octop-turn:{agent}:{thread}`） | ⚠️ **选型对，语义没吃透**。见 §3.A |

---

## 2. 做得对的部分（这些是真生效的）

1. **选主选型 + 半开连接兜底**：`leader.py:40-45` TCP keepalive + `:167-181` 心跳 `SELECT 1`
   带 `wait_for` 超时（默认 15s）——把"TCP 半开导致假活"的接管延迟从"OS 级数分钟"压到秒级。
   这个组合是对的。
2. **过渡期串行化**：`leader.py:87-89, 196-214` 用 `_transition_lock` + 强引用集合 + `_handles_running`
   保证"启动中途丢领导权"时 stop 恰好执行一次，且有对应单测。这类竞态能补上测试是加分项。
3. **连接数从 O(并发流) 收敛到 O(节点)**：`NotifyHub`（每进程 1 条 LISTEN）+ `TurnFetchPool`（默认 8）
   + 队列池 ≈ 13。api 侧连接数不再是扩容变量——这是本次改造最实在的收益。
4. **连接池取消安全（已真修）**：`remote.py:329-360`
   - `except BaseException`（`CancelledError` 是 `BaseException`，`except Exception` 捕不到）；
   - 错误路径**先**回滚槽位 + 入队唤醒哨兵，**再**关连接（避免二次取消跳过唤醒）；
   - `_acquire` 建连期间被取消也回滚预留槽位。
   我逐行核对过配平逻辑（含"从 idle 取出的连接出错时 `_open_count -= 1` 是否正确"——是正确的，哨兵会触发重建）。
5. **取消跨进程接线**：`executor.py:92-107` 的 `cancel_turn` 统一入口，remote 优先走 `RemoteExecutor`
   （`pg_notify`），local 回退 registry。接线收敛得很干净。
6. **角色隔离的抽象**：`leader.py:253-290` `BackgroundGate` 用 `enabled` 表达第三态
   （"集群节点但不跑后台单例"），修掉了"`runner is None` 被当成单机全量启动"这个语义缺陷。
   ——但覆盖面只到 remote 拓扑（见 §5）。
7. **队列 schema 幂等 + advisory lock**：`remote.py:173-195`，注释还解释了"procrastinate
   `schema.sql` 非幂等（裸 `CREATE TYPE`）"。踩过坑才写得出来。
8. **单测实跑通过**：我本地跑 `tests/unit/cluster` + `test_remote_executor.py` + `test_executor.py`
   → **46 passed in 4.72s**（命令见文末）。

---

## 3. 我独立实测复现的问题

### 3.A 🔴 自愈对"活着且正常心跳、但回合跑得久"的作业误重派 → 双跑 + **串行化失效**

**已归档报告 §8.2 已指出"双跑"这一层；我实测补上了更严重的第二层后果。**

**探针**（真 PG scratch 库 + 真 procrastinate 3.10 + 生产类 `AgentWorker.retry_stalled_jobs_once`）：

```
[2] worker#1（存活）认领 job A          -> [(1, 'doing', 'octop-turn:agent-1:thread-1', 1)]
[3] job B（同线程第二条消息）worker#2 认领 -> None        （被 A 的 doing 挡住，符合预期）
[4] 自愈扫描#1（A 很新 + worker 心跳正常） -> retried=0     （正常，无副作用）
[5] 模拟长回合：把 A 的 started 事件 backdate 2200s（> 2100s 上限）
    —— worker#1 仍然存活且心跳正常，什么都没坏
[6] 自愈扫描#2                            -> retried=1     ★ 误重派
[7] worker#2 再次认领 A                   -> job id=1 status=doing worker_id=2
    => 双跑：同一个回合在 worker#1 和 worker#2 上同时执行
[8] worker#1 事后 finish_job              -> [(1, 'succeeded', ..., 2), ...]
    => 作业被置为 succeeded，而 worker#2 还在跑
[9] job B 被 worker#3 认领成功             -> ★ 串行化彻底失效：
       同线程的下一条消息在 worker#2 还没跑完时就开始执行
```

**根因（两处语义叠加）**

1. `remote.py:1156-1166` 用 `get_stalled_jobs(nb_seconds=2100)` 做"运行时兜底"。
   该查询对应 procrastinate `sql/queries.sql:24-37` `select_stalled_jobs_by_started`：
   条件只有 `status='doing'` + `MAX(started 事件) < now() - N 秒`，**完全不看心跳**。
2. `procrastinate_retry_job_v2` 的 `WHERE id = job_id AND status IN ('doing','failed')`
   ——**不校验 worker 是否存活**，所以重派必然成功。
3. 放大器：`procrastinate_finish_job_v1` 的 `WHERE ... AND status IN ('todo','doing')`
   ——旧 worker 事后 finish 仍匹配，把作业直接置 `succeeded` → **锁提前释放**。

**影响**：单回合超过 **35 分钟（2100s）** 即触发。触发后：
- 同一线程出现两条并发流、**同一 LangGraph 线程并发写 checkpoint**；
- 线程串行化（整个 `lock` 机制存在的唯一理由）在该窗口内失效。

**与文档自相矛盾**：`microservice-scaling-1000.md` §9 风险登记写"队列任务可见超时与长回合冲突
（单回合可能 >10min）｜等级：低｜缓解：**心跳续锁**（Procrastinate 原生支持）"——
而实现里的运行时兜底**恰恰绕过了心跳**。这条缓解措施不成立。

**修复方向**（改动很小）：运行时兜底只在 `worker_id IS NULL`（worker 行已被 prune，
即确认真死）时重派；或把阈值抬到明显高于正常回合上限（且与 api 侧 1800s 看门狗的关系要重新推演）。

> ⚠️ 顺带一个**不要误报**的点（我核对过）：心跳型查询的条件是
> `(job.worker_id IS NULL OR worker 已停跳)`，而 `procrastinate_jobs.worker_id` 带
> `ON DELETE SET NULL` —— 所以 worker 行被 prune 掉后孤儿作业**仍能被扫到**。
> 这一条设计是对的，不要当成缺陷。

---

### 3.B 🔴【已归档报告未覆盖】渠道运行时状态只在 leader 生效，且**没有跨节点传播**

**事实**

| # | 事实 | 位置 |
|---|---|---|
| 1 | 非 leader 上 `_safe_register_channel` **直接 return**（只写"standby"状态，不注册） | `gateway.py:662-669` |
| 2 | `reload_channels_from_db`（先注销再全量重注册）只有 **3 个调用点**：选主获得、选主失去、备份恢复 | `server.py:529-538`、`api/routers/backup.py:202` |
| 3 | `create_channel` / `update_channel` / `delete_channel` 只操作**本节点**的 channel manager | `gateway.py:370 / 392 / 401` |
| 4 | 渠道相关**没有任何跨节点通知机制**（gateway 目录下无 channel 类 `pg_notify`/`LISTEN`） | Grep 全仓确认 |

**后果**：管理员在控制台增删改 IM 渠道，请求经 LB 轮询，**落到非 leader 节点的概率是 (N-1)/N**：

- **新建/启用渠道** → leader 不知道 → 渠道不工作（用户看到"已保存"）；
- **删除渠道** → leader 继续轮询 → **"已删除"的渠道仍在消费消息**；
- **修改配置**（如换凭据）→ leader 仍跑旧配置。

直到 leader 重启或领导权转移才自愈。**行为不确定**（取决于 LB 把请求发给哪个节点），排查成本极高。

已归档报告的 R2-5 只提到"状态显示闪烁"（UI 层），实际影响是**功能不生效 / 删不掉**，比 UI 闪烁严重得多。

**修复方向**：渠道变更发 PG NOTIFY，leader LISTEN 后 reload；或把渠道写操作收敛到 leader（follower 转发）。

---

### 3.C 🔴【已归档报告未覆盖】api 崩溃后，在途远端回合无法续接 → "轨迹 RPO=0"不成立

**三个事实（逐行确认）**

1. 轨迹事件**只由 api 侧 processor 写**：`observe_chunk` 唯一调用点 `processor.py:279`
   → worker 只写 `octop_turn_events`，不写轨迹表。
2. 远端回合的 `job_id` 是 `new_ulid()` 现生成的，**不落任何业务表**（`remote.py:706-715`）
   → 其他节点无法查到"这个线程正在跑哪个 job"。
3. 实时推送总线 `TrajectoryLiveBus` 是**纯进程内字典**（`live.py:12`）；
   设计文档 §6 计划的 `TrajectoryLiveBus` 抽象 + `PgNotifyBus` 实现**没有做**（全仓 grep 零命中）。

**后果**：api 节点在生成中途崩溃 / 滚动升级时——

- worker 继续执行（这部分符合文档承诺 ✅）；
- 但**没有任何进程把后续 chunk 写进轨迹表**，也没有任何节点能续接这个 job；
- 浏览器重连（`useTrajectorySession` 用 `EventSource` + `after_seq` 回放）只能回放到崩溃点，
  之后只收到 heartbeat → 用户看到"回放完就卡住"，需手动刷新。

**与文档冲突**：`production-ha.md` §0「会话数据 RPO = 0（会话/轨迹/chunk 全部落 PG），无内存态依赖」、
§2.1「api 重启期间 worker 照常执行、chunk 照常落 DB → **浏览器侧零丢失**」、
§2.6「滚动升级：不中断在途会话」——对 remote 模式不成立（chunk 确实落在 `octop_turn_events`，
但那张表按临时 job_id 索引、且 1800s 就被清理，没有任何查询路径能把它变成用户可见的历史）。

**修复方向**：把 job_id 映射落库（thread → 当前 job_id）以便新节点续接；或实现 `PgNotifyBus`
让 SSE 走"DB 回放 + 跨节点 LISTEN 唤醒"（设计文档 §4.3 原本就是这么设计的）。
**若不做，则应把文档承诺降级为"已落库部分可回放"**。

> 边界：这是**代码推导**结论（三条事实都是逐行确认的），我没有做双节点崩溃实测。

---

### 3.D 🟠 `publisher.open()` 在 try 之外 → 该步失败时读者要等满 30 分钟

`remote.py:872-875`：

```python
publisher = publisher_factory(job_id)
await publisher.open()          # ← 在 try 之外
logger.debug(...)
try:
    ...
except BaseException as exc:
    await publisher.fail(...)   # ← 唯一发错误哨兵的地方
```

`open()` 失败（连接数打满、PG 抖动）时**不会发错误哨兵**，作业被 procrastinate 标记 failed，
而 api 侧读者只能等 `turn_timeout=1800s` 看门狗超时 —— 用户等 30 分钟才看到失败。
修复：把 `open()` 纳入 try，或在 open 失败时直接发哨兵。

---

### 3.E 🔵 `retry_stalled_jobs_once` 无逐作业容错

`remote.py:1148-1165` 逐个 `await job_manager.retry_job(job)`，无 per-job `try/except`。
而 `procrastinate_retry_job_v2` 在作业已不在 `doing/failed` 时会 `RAISE 'Job was not found or has an
invalid status to retry'`。触发场景有两个：① 作业在扫描与重派之间被别的 worker 处理完；
② 多 worker 并发扫描同一作业（N 个 worker 各跑一遍扫描，天然会撞）。后果是**本轮扫描剩余作业全部跳过**
（下一个 60s 周期再补）+ 日志噪音。修复：每个作业包一层 try/except。

---

## 4. 文档承诺 vs 实现对照表

> 这是本类评审最高产的发现来源。下表每一项都逐行核对过。

| # | 文档写了 | 实现 | 状态 |
|---|---|---|---|
| 1 | `octop agent-worker --workspace <路径>`（`deployment-stage2.md:37`，拓扑图 `:51` 同） | CLI 只有 `--concurrency` / `--debug`（`agent_worker.py:53-61`）→ **照抄直接报错** | 🔴 未处理 |
| 2 | worker 启动时预热 checkpointer、健康检查留余量（`deployment-stage2.md:75`） | `AgentWorker` 的 `warmup` 参数**生产无任何调用方**（只有 `scripts/stage2/worker.py` 传）；实际靠 `prestart_agents=True` 间接规避 DDL 死锁 | ⚠️ 效果达成、参数成死代码、文档未同步 |
| 3 | worker 懒加载，"内存 ∝ 该 worker 活跃会话数"（`microservice-scaling-1000.md` §3） | worker `prestart_agents=True` → **启动即加载全部 agent**（`server.py:524` → `manager.py:501-504`）→ 内存回到 O(agent 总数) × worker 数 | ⚠️ **与设计目标相反**（换取 DDL 安全，属取舍但未说明） |
| 4 | LB 健康检查 5s×2 失败摘除（`production-ha.md:21` 等多处拓扑图） | ① nginx 用 `set $octop_api` + `proxy_pass $变量`，**无 upstream 块** → `max_fails`/`proxy_next_upstream` 无处可配（`cluster-lb.conf:15-17`）；② `/api/health` 只有 liveness 语义（`health.py`，`ok` 恒为 True） | 🔴 **双重不成立** |
| 5 | 阶段 0（N 个 `octop run` + LB）后台单例只在 leader —— `scaling-500-users.md` 路线 B 标「**推荐 ✅**」，且全文不含 `OCTOP_EXEC_MODE` | `LeaderRunner` 只在 `execution_mode == "remote"` 内创建（`server.py:383-399`）→ local 多副本每副本都内联跑**全部**后台单例 | 🔴 R2-1 |
| 6 | `run_migrations` 外包 advisory lock（`scaling-500-users.md:68,126`） | `migrate.py` 全文 **`advisory` 零命中**；对比队列 schema 是有锁的（同一作者） | 🔴 R2-2 |
| 7 | 队列串行"禁用 `lock`，用 `queueing_lock`"（`production-ha.md:50`） | 用 `lock` + 自愈 | ✅ **实际做法是对的**（见 §6 说明） |
| 8 | 压测/证据可复现 | 5 个脚本 + 2 个 `result.json` 硬编码本机解释器路径（`/Users/...`） | 🔵 R2-8 |
| 9 | 分层硬禁令 `infra/` → `api/`（`AGENTS.md` §5） | `remote.py:103` `from octop.api.routers.chat.sse import json_chunk_default` | 🔵 R2-7 |

---

## 5. 部署面：编排与 LB

`docker/docker-compose.cluster.yml` 修得不错（P2-8 已修：api 也挂了 `octop_workspace`，
注释改成"集群模式【必须先配 MinIO】"）。剩下两个：

1. **LB 配不了健康检查**（上表 #4）。`cluster-lb.conf` 用变量 + docker DNS 运行时解析，
   换来了"`--scale` 后免重载 nginx"，代价是**没有 upstream 块 = 没有被动健康检查**。
   挂一个 api 容器 → 1/N 请求直接 502（客户端得自己重试）。
   要么改回 upstream + 重载，要么上云 LB / 加 `proxy_next_upstream` 的等价方案。
2. **渠道状态跨节点不传播**（§3.B）——compose 默认 `--scale api=2`，所以这条在默认编排下必然触发。

另外：`/api/health` 连"DB 是否可用"都只是回显 `database_bound`，没有"迁移版本匹配"语义，
所以即使 LB 能配健康检查，也分不出"活着"和"能服务"。

---

## 6. 方案合理性：分维度结论

| 维度 | 结论 | 理由 |
|---|---|---|
| 选主机制 | ✅ 合理 | 会话级 advisory lock 优于 TTL 租约：DB 唯一仲裁、无时钟依赖、无脑裂窗口；半开连接有 keepalive + 心跳超时兜底 |
| 队列选型 | ✅ 合理 | procrastinate 契合"PG 已是硬前提"；回合级下发远低于吞吐瓶颈 |
| 回传通道 | ✅ 合理 | 事件表 + 50ms 批量 NOTIFY，PoC 2 实测达标，不必引 Redis |
| 同线程串行用 `lock` | ⚠️ 选型对、语义没吃透 | **顺带纠正文档**：曾建议改 `queueing_lock` —— 那个建议本身是错的。`queueing_lock` 是"去重"（同锁已有作业时**拒绝入队**），用在用户消息上会**丢消息**；`lock` 才是"串行"。所以"保留 `lock` + 自愈"方向正确，问题出在自愈判据（§3.A） |
| 角色划分 | ⚠️ 抽象对、覆盖面不够 | `BackgroundGate` 的第三态抽象是对的，但只覆盖 remote 拓扑；文档标为"推荐"的阶段 0 形态没有门控（§4 #5） |
| 执行器抽象 | ✅ 合理 | 协议 + 构造器注入，默认行为零变化，调用点零改动——接缝设计得很干净 |
| 可靠性承诺的粒度 | ⚠️ 偏乐观 | 核心机制有真证据，但"RPO=0 / 零丢失 / 滚动升级不中断"建立在"api 节点不会在生成中途死"的隐含假设上（§3.C） |
| 文档与实现同步度 | 🔴 不足 | §4 九项里有 4 项是"文档写了、代码没做"；根因是缺一道"把文档承诺变成可执行断言"的闸 |

**建议补的闸门（都很小）**：
`test_documented_cli_flags_exist`（挡住 #1）、`test_worker_role_starts_no_background_singletons`（挡住 #5）、
`test_migrations_take_advisory_lock`（挡住 #6）、`test_channel_change_propagates_to_leader`（挡住 §3.B）。

---

## 7. 建议的处理顺序（按"会不会真的伤到用户"）

| 顺序 | 项 | 为什么排这个位置 | 量级 |
|---|---|---|---|
| 1 | **§3.A 自愈误重派** | 唯一会破坏"同线程串行"这一核心保证的；收窄重派条件即可 | 小 |
| 2 | **§3.B 渠道状态不传播** | 集群下渠道"增删改看起来成功、实际不生效/删不掉"，用户侧最容易投诉且行为不确定 | 中 |
| 3 | **§4 #5 阶段 0 门控 + #6 迁移锁** | "按你们推荐的方式部署就踩"；两处改动都很小 | 小 |
| 4 | **§3.C api 崩溃续接** | 影响滚动升级承诺。要么实现 `PgNotifyBus` + 落库 job_id，要么**把文档承诺降级**为"已落库部分可回放" | 中 / 文档 |
| 5 | **§4 #4 LB 健康检查** | 要么加 upstream 块（牺牲 `--scale` 免重载），要么上云 LB；同时补 readiness 语义 | 小 |
| 6 | §3.D / §3.E / §4 #1#2#3#8#9 | 收尾 + 文档同步 | 小 |

---

## 8. 诚实边界

**我实测过的**：真 PG 上的队列锁语义与自愈误重派（探针脚本 + 完整输出见 §3.A）；
procrastinate 依赖源码逐条核对（`sql/queries.sql`、`sql/schema.sql`、`manager.py`、`worker.py`）；
46 个相关单测实跑通过；所有 `文件:行号` 引用均为逐行阅读确认。

**我没有实测的（结论来自代码推导 + 文档对照）**：双节点容器下的崩溃 / 滚动升级端到端
（本机未起该 compose）；LB 的 502 比例；§3.B 的渠道跨节点传播（未起双 api 实测）；
§3.C 的 api 崩溃续接（三条事实逐行确认，但未做双节点实测）。

**关于已归档报告**：我把它作为交叉验证来源。它的第一轮 P0-1/P1-2/P1-3/P1-4 结论我逐条核对，
代码现状与"已修复"声明一致；它的 §8.2（双跑）我独立复现并补上了"锁提前释放 → 串行化失效"这一层；
第二轮 9 项里 R2-1/R2-2/R2-3/R2-5/R2-6/R2-7/R2-8 我确认仍未处理，
其中 R2-5 我判断实际影响比报告描述更严重（见 §3.B）。

---

## 附：复现命令

```bash
cd <repo>

# 单测（本沙箱必须把 basetemp 放进工作区，仅设 TMPDIR=/tmp/xxx 已不够）
mkdir -p .workbuddy-ai/tmp/pytest
TMPDIR=.workbuddy-ai/tmp .venv/bin/python -m pytest \
  tests/unit/cluster tests/unit/agents/test_remote_executor.py tests/unit/agents/test_executor.py \
  -q -p no:cacheprovider --basetemp=.workbuddy-ai/tmp/pytest
# -> 46 passed

# 自愈误重派探针（真 PG + 真 procrastinate + 生产类 AgentWorker）
docker exec octop-postgres psql -U octop_admin -d postgres \
  -c "DROP DATABASE IF EXISTS octop_probe_rv" -c "CREATE DATABASE octop_probe_rv"
TMPDIR=.workbuddy-ai/tmp .venv/bin/python .workbuddy-ai/tmp/probe/probe_stalled_double_run.py
```
