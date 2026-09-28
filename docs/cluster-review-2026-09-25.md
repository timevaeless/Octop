# Octop 集群方案（stage 2/3）质量评审

> 评审日期：2026-09-25 ｜ 分支：`feat/direct-portal-oidc`（HEAD `497eaa4a`）
> 被评审提交：`c87304d1`（执行器抽象 + RemoteExecutor）、`1e40cc9e`（选主 + 压测 + 编排）、`1214ea19`（评审修复）
> 证据脚本与日志：`.workbuddy/cluster-review-evidence/`
>
> 结构：**§1–§6 第一轮**（4 个实测缺陷）；**§7 第二轮深挖**（同日补测，新增 9 项，含 2 个 P1）；
> **§8 第三轮修复验证**（验证提交 `c0dc7cc3`）。
> 三轮都没有改动任何源码，仅做调研与实验。

---

## 0. 一句话结论

**架构方向是对的，机制级验证也是真跑过的；但"生产级"这个结论下得偏早。**

有 3 个问题会让集群在生产上"看起来在跑、实际功能是坏的"，其中 1 个会把**用户某个会话线程永久卡死**，
1 个与文档承诺**完全相反**（worker 节点把本该只跑一份的后台任务全跑了），1 个让**"停止生成"按钮在集群下彻底失效**。
三者都不在现有 39 个单测 + stage3 压测的覆盖范围内。

| 状态 | 内容 |
|---|---|
| ✅ 已生效 | 选主机制、共享连接收敛、批量回传、看门狗、幂等建表、冷启动竞态修复 —— 代码在、实测过 |
| ⚠️ 做好了但没接线 | `warmup`（预热）只存在于 PoC 脚本，生产 CLI 没传；`retry_stalled_jobs` 文档自己写了"不配则永久卡死"，但没实现 |
| 🔴 有洞没堵 | 队列锁会让线程永久卡死；worker 角色隔离没生效；集群下取消失效；连接池可被取消操作漏空 |

---

## 1. 问题清单（按严重度）

| # | 级别 | 问题 | 生产影响 | 证据 |
|---|---|---|---|---|
| **P0-1** | 🔴 严重 | 队列锁 `lock=octop-turn:{agent}:{thread}`：worker 崩溃后僵尸作业永久占用同线程锁 | **该用户该会话的所有后续消息永远不执行**（不是文档承诺的 90s 恢复） | 实测复现 |
| **P1-2** | 🔴 严重 | `leader=False` 的 worker 节点被当成"单机模式"，cron / IM / proactive / 知识恢复**全量启动** | N 个 worker + 1 个 api leader 重复跑定时任务、重复推送、重复拉取 IM 消息 | 实测复现 |
| **P1-3** | 🔴 严重 | 集群下"停止生成"失效：WS `cancel` 与 slash `/stop` 直连本地 `AgentManager`，绕过 `RemoteExecutor` | 用户点了停止没反应，worker 继续烧 token | 代码定位 |
| **P1-4** | 🟠 高 | `TurnFetchPool` 在 fetch 被取消时泄漏连接槽位（不回滚、不关闭、不唤醒等待者） | 累计 8 次后 api 侧流式请求**永久阻塞**，需重启进程 | 实测复现 |
| **P2-5** | 🟠 高 | worker 的取消监听器（`_cancel_loop`）连接断了不重连，任务静默死亡 | PG 抖动一次后，该 worker 上"停止生成"永久失效 | 代码定位 |
| **P2-6** | 🟡 中 | `RemoteExecutor.cancel_stream` 在事件循环里做**同步阻塞** `psycopg.connect`（最长 3s） | PG 慢时 api 整个事件循环被卡住 | 代码定位 |
| **P2-7** | 🟡 中 | worker 节点不预热（`prestart_agents=False` 语义错位；`warmup` 未接线） | 首次回合慢；预热本来要规避的 checkpointer DDL 死锁没被规避 | 实测日志 |
| **P2-8** | 🟡 中 | compose 里共享 workspace 卷只挂给 worker，api 未挂 | 注释宣称的"未配 MinIO 前用共享卷"不成立，api 上传的附件 worker 看不到 | compose 对比 |
| **P3-9** | 🔵 低 | 文档与代码/文档与文档互相矛盾 | 运维按文档调参会踩坑 | 引用对照 |

---

## 2. 详细分析

### P0-1 队列锁会让线程永久卡死（🔴 最高优先级）

**根因**
`src/octop/infra/agents/remote.py:593-595, 614` 用 procrastinate 的 `lock` 字段做同线程串行：

```python
@staticmethod
def _thread_lock(agent_id, thread_id): return f"octop-turn:{agent_id}:{thread_id}"
...
await queue.defer(payload, lock=self._thread_lock(agent_id, thread_id))
```

procrastinate 的 `lock` 语义是**行级唯一索引**（`procrastinate_jobs (lock) WHERE status='doing'`）：
只要有同锁作业停在 `doing`，后续同锁作业**永远取不到**（`schema.sql` 的 `procrastinate_fetch_job_v2` 判定）。
而 worker 崩溃时，procrastinate 只会 `prune_stalled_workers` 删掉 worker 行（`schema.sql:553-563`），
**不会**把它的 `doing` 作业改回 `todo` —— 这一步必须由调用方周期跑 `retry_stalled_jobs`。

**本项目自己早就写明了这个坑，但没做：**

- `docs/production-ha.md:50-51`：「**禁用 `lock` 字段**——社区生产案例（LeanIX）证实 worker 崩溃 + lock 会死锁，本方案不使用。」
- `docs/production-ha.md:140`：「`retry_stalled_jobs` 周期任务 … **不配则崩溃任务永久卡死**」
- 全仓库 grep：`retry_stalled_jobs` / `get_stalled_jobs` / `job_timeout` **在 `src/` 里零引用**。

**实测复现**（`.workbuddy/cluster-review-evidence/probe_lock_wedge.py`，真 PG + 真 procrastinate）：

```
[2] worker 认领 A -> (1, 'doing')
[3] 模拟 worker 崩溃：prune_stalled_workers(0) 已删掉该 worker 行
    当前作业表: [(1, 'doing', 'octop-turn:agent-1:thread-1', None)]   <-- 僵尸作业留在 doing
[4] 投递同 lock 的 job B（用户下一条消息）
    当前作业表: [(1, 'doing', ...), (2, 'todo', ...)]
[5.1] 另一存活 worker 第 1 次尝试认领 job B -> (None, None)
[5.2] 另一存活 worker 第 2 次尝试认领 job B -> (None, None)
[5.3] 另一存活 worker 第 3 次尝试认领 job B -> (None, None)
[6] get_stalled_jobs() 返回 1 个僵尸作业: [1]
[7] retry_job 后作业表: [(1, 'todo', ...), (2, 'todo', ...)]
[8] 手工 retry 后再次认领 -> (1, 'doing')（此时才恢复）
```

**影响**
- 触发条件很常见：容器 OOM、节点宕机、`docker stop` 超时被 SIGKILL、滚动升级中进程被强杀。
- 触发后：该 `(agent, thread)` 的所有后续消息**永远排队不执行**；api 侧读者等 1800s 超时后报错，
  用户看到的是"每次发消息都卡 30 分钟然后失败"。
- 因为 `job_timeout` 也没配，**worker 假活（没崩但卡住）同样永久占锁**，不只是崩溃场景。
- 严重度评估：这是"单点故障扩散成用户数据面不可用"，且**无自愈**。

**修复方向**（三选一或组合）
1. 按自己的文档改成 `queueing_lock`（需在 api 侧处理 `AlreadyEnqueued`）；
2. 补上 `retry_stalled_jobs` 周期任务（1min）+ 设 `job_timeout`；
3. 更稳的做法：把串行化搬到应用层（PG advisory lock + 自己的 turn 表），不完全依赖队列锁语义。

---

### P1-2 worker 节点把后台单例全跑了（🔴 与文档承诺相反）

**文档承诺**（`docs/deployment-stage3.md:21`）：
> **角色边界**：api 节点参与选主；agent-worker 以 `leader=False` 启动，永不竞争——**后台单例只落在 api 节点**。

**代码实际行为**（`src/octop/infra/server.py:396-407, 554`）：

```python
if config.execution_mode == "remote":
    remote_executor = RemoteExecutor(dsn=config.queue_dsn)
    if self._leader_enabled:                       # worker 是 False
        leader_runner = LeaderRunner(config.queue_dsn)   # -> 仍是 None
gate = BackgroundGate(leader_runner)               # -> BackgroundGate(None)
```

而 `src/octop/infra/cluster/leader.py:276-278` 把 `runner is None` 解释为**"单机部署，立即启动"**：

```python
if self._runner is None:
    await _as_async(start)()      # 立刻启动，不选主
    return
```

**根因**：`BackgroundGate` 把"没有 leader runner"等同于"单机部署"，无法表达"我是集群节点，但我不该跑后台任务"。

**实测复现**：用真实 `octop agent-worker`（`OCTOP_EXEC_MODE=remote` + 独立 scratch DB）启动，
日志（`.workbuddy/cluster-review-evidence/worker_boot.log`）：

```
line 9   INFO apscheduler.scheduler: Scheduler started
line 10  INFO octop.infra.cron.manager: CronManager booted; scheduled 0 jobs     <-- cron 调度器
line 14  INFO octop.infra.proactive.scheduler: ProactiveCareScheduler: started   <-- 主动关怀
line 15  INFO octop.infra.gateway.gateway: Gateway channels reloaded from DB     <-- IM 渠道
line 13  INFO octop.infra.agents.manager: prestart_agents=False (remote api role) <-- 见 P2-7
```

（探针库为空所以是 "0 jobs / 0 enabled"；DB 里有数据时这些任务会**真的执行**。）

**影响**
- `cron`：N 个 worker + 1 个 api leader 同时触发定时任务 → 备份、TLS 续期、用户自定义 cron agent **重复执行 N+1 次**。
- `proactive-care`：同一用户收到 N+1 条主动推送。
- IM 渠道：多个进程同时用同一份凭据长连接轮询 → 重复消费消息；更糟的是 worker 的 Gateway 注入的是
  `RemoteExecutor`（`server.py:455`），它会**再入队一次**，形成消息重复链路。
- 幂等性没有兜底：投递状态在 repo，但"重复推送/重复执行"本身不是幂等语义。

**修复方向**：给 `BackgroundGate` 增加显式的"本节点不跑后台任务"状态
（如 `BackgroundGate(runner, enabled=False)`，或 `OctopServer(background=False)`），
让 worker 走"什么都不启动"的第三条分支，而不是被当成单机。

---

### P1-3 集群下"停止生成"失效

**代码**（`src/octop/api/routers/chat/ws.py:127`）：

```python
server.app_runtime.agent_registry.cancel_stream(agent_id, thread_id)
```

以及 slash `/stop`（`src/octop/infra/gateway/slash/runtime_bridge.py:38-41`）同样直连 `agent_manager.cancel_stream`。

在 remote 模式下，回合跑在 worker 上，api 进程里**没有任何 harness 流**，
所以这两处调用只会去取消一个不存在的本地流 —— 静默无效。
真正能跨进程取消的路径只有 `gateway.py:534`（`self._executor.cancel_stream`，走 `pg_notify`），
而它只在"发新消息抢占旧流"时被调用，**不是用户点停止的路径**。

**这不是隐藏 bug，而是明确遗留但没闭环**：
`docs/microservice-scaling-1000.md:139-141` 写着"取消跨服务化的接线归入阶段 2"，
但阶段 2/3 都已宣告落地，而阶段 2 的退出标准（`:156`）明确包含"**取消**…通过"。

**影响**：集群下用户无法中断正在生成的回合；worker 继续烧 token，直到自然结束。
（注：api 侧 SSE 断连不会自动取消远端回合 —— 本地模式也是这个语义，属于一致行为，不算回归。）

---

### P1-4 连接池被"取消"漏空 → api 流式永久阻塞

**代码**（`remote.py:311-337`）：

```python
async def fetch(self, sql, params):
    conn = await self._acquire()
    try:
        ... await cur.execute(...) ; rows = await cur.fetchall()
    except Exception:                      # <-- 只捕 Exception
        self._open_count -= 1
        await conn.close(); raise
    self._idle.put_nowait(conn)            # <-- 没有 finally
    return rows

async def _acquire(self):
    if self._idle.empty() and self._open_count < self._size:
        self._open_count += 1              # <-- 先占坑
        return await self._connect(...)    # <-- 取消时坑位不回滚
    return await self._idle.get()          # <-- 无超时，无人唤醒则永久阻塞
```

`asyncio.CancelledError` 是 `BaseException`，`except Exception` 捕不到：
- 在 `fetch` 的 DB 往返期间被取消 → 连接不归还、不关闭、`_open_count` 不回滚；
- 在 `_acquire` 建连期间被取消 → 坑位永久占用；
- 若此时还有别的协程阻塞在 `_idle.get()`，丢连接的一方**不会唤醒等待者** → 永久挂起。

**实测复现**（`.workbuddy/cluster-review-evidence/probe_fetch_pool_leak.py`，size=1）：

```
[1] 1 路 fetch 在途: _open_count=1 idle=0
[2] 模拟客户端断连：fetch 任务被取消
[3] 取消后: _open_count=1 idle=0 conn.closed=False
    -> 槽位未回滚、连接未关闭（永久泄漏）
[4] 后续请求尝试取连接（3s 超时）...
    -> 超时：池已枯竭，后续所有流式请求永久阻塞
```

**影响**：默认 `pool_size=8`。用户频繁关闭页面/断网（SSE 取消）时，
累计 8 次落在窗口内即**整节点流式能力永久丧失**，只能重启进程。没有自愈、没有超时、没有告警。

**修复方向**：`fetch` 用 `try/finally` + `except BaseException` 归还/关闭并回滚计数；
`_acquire` 的建连也包一层取消回滚；`_idle.get()` 加超时或改为 `asyncio.Condition`/信号量实现，
丢连接时主动唤醒等待者。

---

### P2-5 worker 取消监听器断连即死

`remote.py:944-957` 的 `_cancel_loop` 没有重连：

```python
listener = await psycopg.AsyncConnection.connect(...)
try:
    await listener.execute(f"LISTEN {CANCEL_CHANNEL}")
    async for notify in listener.notifies():   # 连接一断，异常直接冒出去
        ...
finally:
    await listener.close()
```

对比同文件的 `NotifyHub._dispatch_loop`（`:238-258`）是有重连的 —— 说明作者知道这个模式，只是漏了这处。
任务异常后不会重启（`create_task` 的异常没人 await），于是该 worker **永久不再响应取消**，
用户表现为"某些时候停止有效、某些时候无效"（取决于消息被哪个 worker 接走），极难排查。

---

### P2-6 取消路径阻塞事件循环

`remote.py:657-664`：

```python
def cancel_stream(self, agent_id, thread_id) -> None:
    with psycopg.connect(self._dsn, autocommit=True, connect_timeout=3) as conn:
        conn.execute("SELECT pg_notify(%s, %s)", ...)
```

同步 psycopg 建连（TCP 握手 + 认证）最长 3s，被同步调用在 async 链路上
（`ws.py` 在 async 处理函数里调用）。PG 抖动时会把整个 api 事件循环卡住，
比"取消失败"本身影响面更大。应改为复用已有的异步连接/队列（例如借 `NotifyHub` 的连接发通知）。

---

### P2-7 worker 不预热（warmup 未接线）

- `AgentWorker` 支持 `warmup` 参数（`remote.py:854, 874-880`），注释明确写了它要防
  "langgraph `CREATE INDEX CONCURRENTLY` 与 procrastinate 长事务死锁"；
- 但生产 CLI `src/octop/cli/commands/agent_worker.py:39-44` **没有传 `warmup`** ——
  只有 PoC 脚本 `scripts/stage2/worker.py:103-148` 传了；
- 同时 `server.py:520` `registry.boot(prestart_agents=remote_executor is None)`：
  因为 worker 也开着 `OCTOP_EXEC_MODE=remote`，所以 worker 拿到 `False`，
  日志原文就是 `prestart_agents=False (remote api role)` —— 但 worker 恰恰是**唯一执行回合的角色**，
  这个判断把"api 角色"和"worker 角色"混为一谈了。

**影响**：worker 首次回合承担懒初始化开销；预热要规避的 DDL 死锁风险在生产路径上没有消除。

---

### P2-8 compose 的共享卷与注释不符

`docker/docker-compose.cluster.yml:94-95` 只给 `worker` 挂了 `octop_workspace`，
`api`（`:59-77`）没有 `volumes`。而注释（`:15-16`）写的是
「未配置 [MinIO] 前 compose 用共享卷 `octop_workspace`，多副本仅限单宿主机」。
实际上 api 侧的上传/附件会落到 **api 容器自己的临时盘**，worker 看不到 —— 这个 fallback 不成立。
集群模式必须先配对象存储（文档也是这么要求的），但 compose 给人的印象是"不配也能跑单宿主机多副本"。
建议：要么给 api 也挂同一个卷，要么把注释改成"必须先配 MinIO"。

---

### P3-9 文档内部矛盾（会让运维踩坑）

| 主题 | 说法 A | 说法 B | 代码实际 |
|---|---|---|---|
| 队列串行用哪个锁 | `production-ha.md:50` **禁用 `lock`** | `microservice-scaling-1000.md:169` 用 `lock`；`:261,299` 又说 `queueing_lock` 是硬性要求 | 用 `lock`（→ P0-1） |
| Follower 轮询间隔 | `production-ha.md:23`、`deployment-stage3.md:19` **5s** | `leader.py:67` 默认 **10.0s** | 10s |
| worker 角色 | `deployment-stage3.md:21` 后台单例只在 api | 代码 `BackgroundGate(None)` = 单机 | worker 全跑（→ P1-2） |

---

## 3. 做得好的部分（这些是真生效的）

1. **选主机制选得对**：会话级 advisory lock（`leader.py:137`）而不是 TTL 租约 —— 进程死 = 锁自动释放，
   无时钟依赖、无脑裂窗口，这个判断是对的；`_watch_session` 还加了 `wait_for` 超时 + TCP keepalive 兜底半开连接（`:167-181, 40-45`）。
2. **过渡期串行化**：`_transition_lock` + 强引用 + `_handles_running` 保证 stop 恰好一次（`:87-89, 196-214`），
   确实修掉了"启动中途丢领导权 → 半启动服务被并发 stop"的竞态，且有对应单测。
3. **连接数收敛**：`NotifyHub`（每进程 1 条 LISTEN）+ `TurnFetchPool`（默认 8）
   把 api 侧连接从 O(并发流) 降到 O(节点数)，压测实测 executor 侧 13 条、hub 1 条 —— 这个改造有效。
4. **冷启动竞态修复**：`_ensure_open` 加 `asyncio.Lock`（`:572-591`），并有
   `test_concurrent_cold_start_creates_single_*` 双测锁定 —— 这类竞态能补上单测是加分项。
5. **看门狗 + 幂等建表 + 分批清理**：`turn_timeout`（1800s 无事件报错）、
   `ensure_queue_schema` 用 advisory lock 规避 procrastinate 非幂等 `CREATE TYPE`、
   `prune_turn_events` 分批 5000 条 —— 都是有经验的写法。
6. **执行器抽象**（`executor.py`）：`AgentExecutor` 协议 + `InProcessExecutor` 纯委托，
   5 个调用点构造器注入，默认行为零变化 —— 为后续扩展留的口子很干净。
7. **单测质量**：39 个 cluster/remote 单测全过（本地实跑 `39 passed in 3.77s`），
   假 PG 会话模拟选主语义的写法是合理的。

---

## 4. 测试与文档覆盖缺口

现有验证（`scripts/stage3/result.json`，verdict PASS）覆盖了：
40 路并发吞吐、首 chunk 延迟、PG 连接上界、事件清理、leader 停止后接管。

**没覆盖的恰恰是上面 P0/P1 的触发场景**：

| 缺口 | 对应问题 |
|---|---|
| worker 崩溃后同线程能否继续 | P0-1（压测只测了 leader 故障切换，没测 worker 故障） |
| `leader=False` 节点的后台单例是否被抑制 | P1-2（没有任何测试断言这件事） |
| remote 模式下 WS `cancel` / slash `/stop` 是否到达 worker | P1-3 |
| 流被取消时连接池是否归还 | P1-4 |
| PG 抖动后取消监听器是否恢复 | P2-5 |

建议补的回归测试（都很小）：
`test_worker_role_starts_no_background_singletons`、
`test_fetch_pool_returns_connection_on_cancel`、
`test_cancel_loop_reconnects_after_connection_loss`、
`test_ws_cancel_routes_through_executor`、
以及一个真 PG 的 `test_worker_crash_does_not_wedge_thread`。

---

## 5. 建议的修复顺序

1. **P0-1 锁语义**（数据面可用性，先堵洞）：短期补 `retry_stalled_jobs` 周期任务 + `job_timeout`；
   中期改成 `queueing_lock` 或应用层串行化。**建议同时加一条监控：`doing` 状态超过 N 分钟的作业数。**
2. **P1-2 角色隔离**：给 `BackgroundGate` 加"本节点不跑后台任务"的显式状态，worker 走该分支。
   这条改起来最小，但影响最大（重复推送/重复执行）。
3. **P1-3 取消接线**：把 `ws.py` / `runtime_bridge.py` 两处改为走 `executor.cancel_stream`。
4. **P1-4 连接池**：`try/finally` + 捕获 `BaseException` + 唤醒等待者。改动小、收益大。
5. **P2-5/6/7/8 + P3-9 文档对齐**：一并收尾。

---

## 6. 评审方法说明

- 代码走读：`infra/cluster/leader.py`、`infra/agents/remote.py`、`infra/server.py`、`gateway/gateway.py`、
  `api/routers/chat/ws.py`、`cli/commands/agent_worker.py`、`docker/*`。
- 三方对照：实现 vs `docs/production-ha.md` / `deployment-stage3.md` / `microservice-scaling-1000.md`。
- 实测：
  - `probe_lock_wedge.py`（真 PG `octop-postgres` + 真 procrastinate 3.10.0）→ P0-1；
  - `run_worker_probe.py`（真 `octop agent-worker` 启动，独立 scratch DB）→ P1-2、P2-7；
  - `probe_fetch_pool_leak.py`（假连接 + 真取消）→ P1-4；
  - `pytest tests/unit/cluster tests/unit/agents/test_remote_executor.py tests/unit/agents/test_executor.py` → 39 passed。
- 未做：真多容器 `docker compose --scale` 编排下的端到端复测（本机 Docker 环境未启动该 compose）。

---

## 7. 第二轮深挖（同日补测）

> 第二轮换角度复查：**部署形态覆盖面**、**控制面迁移的并发安全**、**文档承诺 vs 实测**、
> **LB/前端一致性**、**分层与可复现性**。
> 结论：**没有新的 P0**；但新增 2 个 P1，且这 2 个与第一轮的 P0-1/P1-2 是同一个模式——
> **文档明确写了要求，代码没做，也没有测试拦住**。

### 7.0 第二轮清单

| # | 级别 | 问题 | 影响 | 证据 |
|---|---|---|---|---|
| **R2-1** | 🔴 严重 | 阶段 0「多完整实例」形态**完全没有门控**：选主只在 `execution_mode=remote` 时创建 | 按官方推荐的 500 用户方案部署 N 副本 → 每副本都跑后台单例 | 代码 + 文档对照 |
| **R2-2** | 🔴 严重 | 控制面 `run_migrations` **无跨进程互斥**（队列 schema 有锁，控制面没有） | `--scale` 同时启动时并发迁移 → 后到节点启动失败/抖动 | 代码 + 文档对照 |
| **R2-3** | 🟠 高 | 文档给的 worker 启动命令**不存在该参数** | 照抄文档命令直接报错 | 实测 |
| **R2-4** | 🟠 高 | 文档承诺"worker 启动时会预热"，实测**没有任何预热动作** | 预热要规避的 DDL 死锁风险仍在 | 实测日志 + 文档 |
| **R2-5** | 🟡 中 | IM 渠道连接状态是**节点内内存态**，经 LB 轮询暴露 | 前端"已连接/未连接"来回闪 | 代码 |
| **R2-6** | 🟡 中 | 集群 LB **配不了健康检查**，与各文档拓扑图/HA 表不一致 | 挂一个 api，1/N 请求直接 502 | 配置语义 + 文档对照 |
| **R2-7** | 🔵 低 | `infra/` 反向 import `api/`（分层违规，本次改造引入） | 架构约束被打破 | 全仓 grep |
| **R2-8** | 🔵 低 | 压测/证据脚本硬编码本机 Python 路径 | 别人 clone 下来跑不了"证据" | 全仓 grep |
| **R2-9** | 🔵 低 | 冷启动无预热 + 事件回传无定时 flush + 读取无 LIMIT | 首个请求慢、长静默时前端卡住 | 代码 |

---

### R2-1 阶段 0 的多副本形态没有门控（🔴）

**文档推荐路线**（`docs/scaling-500-users.md:40-57`，标题写着"推荐 ✅"）：

> ### 路线 B：多完整实例 + Leader 租约（推荐）✅
> 保持 `octop run` 单进程模型不变，跑 N 个完整实例（容器）+ LB。
> **后台单例（cron/gateway IM 连接/主动关怀/备份/知识索引恢复/TLS 续期）只在 Leader 上运行**。
> 门控方式：`_boot_runtime()` 中给单例启动加 `if await lease.hold("background"): ...`

该文档**全文没有出现 `OCTOP_EXEC_MODE` / `execution_mode` / `agent-worker`**（grep 零命中）——
即：这条路线就是"默认 local 模式 + N 副本"，不需要任何新环境变量。

**代码实际**（`src/octop/infra/server.py:383-407`）：

```python
if config.execution_mode == "remote":        # <-- 只有 remote 才建 runner
    ...
    if self._leader_enabled:
        leader_runner = LeaderRunner(config.queue_dsn)
gate = BackgroundGate(leader_runner)          # local 模式 -> BackgroundGate(None)
```

配合 `leader.py:276-278`（`runner is None` ⇒ 立即启动），结论是：
**默认 local 模式下跑 N 副本，N 个副本都会启动全部后台单例。**
这正是 `scaling-500-users.md:126` 列为 P1 要解决的问题（"双实例起停 Leader，cron 只触发一次、IM 只连一次、备份不重复"），
但实现只覆盖了 remote 拓扑。

**影响**：按官方 500 用户方案部署（2 副本、不加任何环境变量）→ cron 双跑、IM 双连、
主动关怀双推送、备份双执行。且这类重复**不会报错**，只会在用户侧表现为"消息重复/推送重复"，
排查成本极高。

**根因与第一轮 P1-2 相同**：`BackgroundGate` 只有两种状态（"有 runner → 挂到 leader" /
"无 runner → 当单机"），缺少第三种"我是集群节点但我不该跑后台任务"。
R2-1 和 P1-2 应当用同一个修复解决。

---

### R2-2 控制面迁移没有跨进程互斥（🔴）

**代码**（`src/octop/infra/db/migrate.py:1941-1986`）：`run_migrations` 全流程无任何锁
（`grep -E "advisory|lock" migrate.py` 零命中）。

对比：队列 schema 是**有**锁的（`remote.py:163-185` `ensure_queue_schema` 用 `pg_advisory_lock` 串行化，
注释还专门解释了"procrastinate schema.sql 非幂等，多节点并发启动会撞车"）。
也就是说——**同一个作者想到了队列 schema 的并发问题，但没把同样的保护用到控制面迁移上。**

**文档明确要求过**（`docs/scaling-500-users.md:68`）：

> | 迁移并发竞争 | ✅ 简单 | `run_migrations` 外包 LeaderLease 或 PG advisory lock；滚动升级时 Leader 先迁完，其余实例起才开始服务 |

`:126` 的 P1 清单里也写了"+ **迁移锁**"。

**触发场景**：`docker compose up -d --scale api=2 --scale worker=2`（文档推荐命令）
→ 4 个进程同时 `run_migrations`。两个节点同时读到旧 `_schema_version` → 同时执行同一个 `.sql` →
后到的节点在 `CREATE TABLE` / `CREATE INDEX` 上撞重复对象 → 事务回滚 → `server.start()` 抛错 → 进程退出。
`restart: unless-stopped` 会重启并在下一轮成功，所以表现为**启动抖动 + 一屏报错**，而不是数据损坏
（迁移文件包在事务里，`migrate.py:1949`）。但风险不止于此：`_ensure_published_experts_schema` 会
**从零重建 `published_experts`**（`migrate.py:1981-1983` 注释自述），并发重建时另一节点正在读该表，
后果就不是"启动失败"这么轻了。

**注意**：这条与第一轮的 worker 角色缺陷叠加会放大——`--scale worker=4` 时，4 个 worker 也在跑迁移。

---

### R2-3 文档里的 worker 命令跑不起来（🟠）

`docs/deployment-stage2.md:37`：

```bash
export OCTOP_QUEUE_DSN=postgresql://user:pass@pg-host:5432/octop
octop agent-worker --workspace <workspace 路径>
```

实测（本机真 CLI）：

```
$ .venv/bin/octop agent-worker --workspace /tmp/x
Usage: octop agent-worker [OPTIONS]
Try 'octop agent-worker --help' for help.
Error: No such option '--workspace'.
```

CLI 实际只有 `--concurrency` / `--debug`（`agent_worker.py:51-59`）。
**影响**：照抄文档部署的人第一步就卡住；而 workspace 路径恰恰是集群模式的关键配置项
（`:42` 明确说"workspace 必须多 worker 共享"），文档让人以为可以按进程指定，实际只能靠环境/控制台配置。

---

### R2-4 文档承诺的 worker 预热不存在（🟠，第一轮 P2-7 的文档侧证据）

`docs/deployment-stage2.md:75`：

> 2. **worker 预热**：worker 启动时会预热 LangGraph checkpointer（避免 `CREATE INDEX CONCURRENTLY`
>    与队列连接死锁），启动到就绪有几秒延迟，健康检查应留余量。

实测：真实 `octop agent-worker` 启动日志（`.workbuddy/cluster-review-evidence/worker_boot.log`）
**没有任何预热动作**——从 `procrastinate schema already installed` 直接到 `Starting worker on queues`，
中间没有 checkpointer 初始化。代码侧对应 `agent_worker.py:39-44` 未传 `warmup`。

这条比第一轮更有说服力：第一轮只是"`warmup` 参数没接线"，现在是
**文档明确承诺 + 实测日志正面否证**。运维按文档"给健康检查留几秒余量"去配置，
得到的却是"首个回合现场初始化"，首次延迟不可预测。

---

### R2-5 IM 渠道状态在集群下会来回闪（🟡）

渠道连接状态是 **Gateway 进程内内存字典**（`gateway.py:132` `self._runtime_status`），
经 `api/routers/channels.py:87` → `runtime_status_to_dict` 暴露给前端。

`get_runtime_status`（`gateway.py:201-217`）对 `reason` 做了一次实时校准，
但 `"standby"` **不在 bypass 列表**里（bypass 只有 `disabled` / `unregistered`），
于是 follower 节点上 `_channel_manager.get_channel()` 返回 None → 跳过校准 →
直接返回 `connected=False, reason="standby", detail="not leader"`。

**结果**：dashboard 每次刷新被 LB 随机路由到某个 api 节点 →
leader 上显示"已连接"，follower 上显示"未连接（备用节点）" →
**同一页面在两种状态之间跳变**。用户会以为渠道坏了。

修法建议：状态要么落库（DB 为单一事实源），要么 follower 返回"由其他节点承载"而不是"未连接"，
或者该接口明确只向 leader 取状态。

---

### R2-6 集群 LB 配不了健康检查（🟡）

`docker/cluster-lb.conf` 为了"`--scale` 扩缩容后无需重载 nginx"，用的是
`resolver 127.0.0.11` + `set $octop_api http://api:8088; proxy_pass $octop_api;`。
这种写法**没有 `upstream` 块**，而 nginx OSS 的被动健康检查
（`max_fails` / `fail_timeout`）和失败重试（`proxy_next_upstream`）都是 `upstream` 的指令 ——
**在这个配置里没有地方可配**。某个 api 容器挂掉时，nginx 仍会把 1/N 的请求发过去，
连接被拒后直接返回 502，客户端只能自己重试。

而各文档的假设不是这样：

| 文档 | 说法 |
|---|---|
| `production-ha.md:21` | 「octop-api ≥2 ｜ **LB 健康检查（建议 5s×2 失败摘除）** ｜ LB 摘除；客户端 SSE 自动重连 ｜ RTO 10–30s」 |
| `scaling-500-users.md:80` | 拓扑图：`LB / nginx（TLS 终结、轮询、健康检查 /api/health）` |
| `deployment-minio-pgvector.md:33` | 同上 |
| `microservice-scaling-1000.md:38` | 同上 |
| `production-ha.md:139` | 自己承认：readiness 探针（第 1 项）**尚未落地**，`/api/health` 只有 liveness 语义 |

**诚实边界**：我没有跑真实的 2-api + nginx 部署去实测 502 比例，这条是
「配置语义 + 文档对照」的结论。建议在修复 P0-1 之后做一次双容器实测确认。

---

### R2-7 分层违规：`infra/` 反向依赖 `api/`（🔵）

```python
# src/octop/infra/agents/remote.py:93
from octop.api.routers.chat.sse import json_chunk_default  # noqa: PLC0415
```

全仓 `grep -E "^\s*(from|import)\s+octop\.(api|cli)\b" src/octop/infra/` **仅此一处**，
且由本次集群改造引入（`c87304d1` 新增文件）。

`AGENTS.md` §5 的硬禁令明确写着：

> **Hard bans** — `infra/` → `api/`, `cli/`, or `launch.py`.

`json_chunk_default` 是纯序列化兜底函数，应该下沉到 `infra/utils/`（或 `infra/i18n` 同级的公共位置），
由 `api/routers/chat/sse.py` 反向引用，而不是让领域层依赖 HTTP 层。

---

### R2-8 压测/证据脚本不可复现（🔵）

6 个脚本硬编码了**本机 WorkBuddy 沙箱**的 Python 解释器路径：

```
scripts/poc1/run_poc.py:160-161   /Users/timevaeless/.workbuddy/binaries/python/envs/pgserver39/bin/python
scripts/poc2/run_poc2.py:162      同上
scripts/poc3/run_poc3.py:47       同上
scripts/stage2/run_stage2.py:97   同上
scripts/stage3/run_stage3.py:45   同上
scripts/{poc1,stage2}/result.json 里也落了本机绝对路径（dsn / workspace）
```

这套脚本是阶段 2/3"全链路压测 PASS"的**唯一证据来源**，但除了作者本人机器，别人跑不起来。
对一个要交付给客户自托管的产品，"生产级验证不可复现"本身是质量项。
建议：改为读环境变量（如 `OCTOP_TEST_PYTHON`）或复用当前解释器 `sys.executable`，
并把 `result.json` 里的绝对路径脱敏为相对路径。

---

### R2-9 三个小问题（🔵）

1. **队列没有启动预热**：`server.py:391` 只构造 `RemoteExecutor`，队列在**首个回合**才惰性 open
   （`remote.py:672-691` `_ensure_open`）。冷启动要做建表检查 + procrastinate App 初始化，
   而 `_ensure_open` 的锁把这些成本串行化 —— 与他们自己压测里
   `first_chunk p50 1.26s / p95 3.25s` 的尾差一致（并发首波都在等第一个 open）。
   在 `_boot_runtime` 里预热一次即可消除。

2. **事件回传没有定时 flush**：`TurnEventPublisher.publish()`（`remote.py:710-715`）
   只在"来了新 chunk 且距上次 flush ≥50ms"时才落库，没有后台定时器。
   若流中途出现长静默（例如一个 60s 的工具调用），**静默前最后一个 token 会滞留到下一个 chunk
   或 END 才写库**，前端表现为"卡住不动"。修法：加一个 50ms 的定时 flush 任务，或 publish 后
   无条件安排一次延迟 flush。

3. **读取端没有 LIMIT**：`TurnStreamReader` 的
   `SELECT seq, payload FROM octop_turn_events WHERE job_id = %s AND seq > %s ORDER BY seq`
   （`remote.py:434-435`）没有 `LIMIT`，读取端落后较多时会一次性把全部积压捞进内存。
   正常路径下每次只差几行，属健壮性缺口。

---

### 7.10 第二轮的质量评价

**加分项**：第一轮里我认为"作者有经验"的判断，第二轮基本被印证——队列 schema 的
advisory lock 注释、`_ensure_open` 的冷启动修复、压测里的连接归因（按 `application_name` 分进程统计），
这些都是踩过坑才写得出来的东西。

**扣分项集中在一个模式**：`docs/` 里有三份相当扎实的方案文档（`production-ha.md` 的故障模式审计、
`scaling-500-users.md` 的路线对比、`microservice-scaling-1000.md` 的 PoC 记录），
**文档的质量明显高于实现与文档的同步度**：

| 文档写了 | 实现 |
|---|---|
| 禁用 `lock`，用 `queueing_lock`（`production-ha.md:50`） | 用 `lock`（第一轮 P0-1） |
| 不配 `retry_stalled_jobs` 则崩溃任务永久卡死（`:140`） | 没配（第一轮 P0-1） |
| 迁移并发竞争要加锁（`scaling-500-users.md:68,126`） | 没加（R2-2） |
| worker 启动时预热 checkpointer（`deployment-stage2.md:75`） | 没预热（R2-4） |
| 后台单例只在 api 节点（`deployment-stage3.md:21`） | worker 全跑（第一轮 P1-2） |
| `octop agent-worker --workspace`（`deployment-stage2.md:37`） | 无此参数（R2-3） |
| LB 健康检查 /api/health（多份拓扑图） | LB 无法配健康检查（R2-6） |

**建议在流程上加一道闸**：把这类"文档承诺"转成可执行的断言测试
（例如 `test_worker_role_starts_no_background_singletons`、`test_documented_cli_flags_exist`、
`test_migrations_take_advisory_lock`）。这些测试都只有几行，但能挡住上面整张表。

---

## 8. 第三轮：修复验证（提交 `c0dc7cc3`）

> 验证日期：2026-09-25 ｜ 被验证提交：`c0dc7cc3`（`fix(cluster): 评审 P0/P1 全量修复——锁自愈+角色隔离+取消跨进程+池取消安全`）
> 方法：**逐项重跑第一轮的复现实验**（真 PG、真进程），而不是只读 diff。命令与日志见 `.workbuddy/cluster-review-evidence/`。

### 8.1 逐项结论

| # | 第一轮问题 | 结论 | 实测证据 |
|---|---|---|---|
| **P0-1** | 队列锁僵尸作业永久卡死 | ✅ **已修复** | `verify_p0_1_selfheal.py`：模拟 worker 崩溃 → 同 lock 的 job B 认领 `(None,None)`（复现卡死）→ `retry_stalled_jobs_once()` 重派 1 个 → 作业回到 `todo` → 再认领 `(1,'doing')` 恢复 |
| **P1-2** | worker 节点把后台单例全跑了 | ✅ **已修复** | `worker_boot_after_fix.log`：`background singleton 'cron-scheduler'/'proactive-care'/'gateway-im-channels'/'knowledge-recovery' skipped: node runs no background tasks` 四条齐全 |
| **P1-3** | 集群下"停止生成"失效 | ✅ **已修复** | 新增 `cancel_turn()` 优先走 `remote_executor`；`test_cancel_turn_routes_to_remote_executor_first` 断言 `calls == ["remote"]`，本地回退为 `["remote","local"]` |
| **P1-4** | 连接池被取消漏空 | ✅ **已修复** | `verify_p1_4_pool.py`：取消后 `_open_count=0 idle=1 conn.closed=True`（槽位回滚 + 连接关闭）；后续 fetch 正常返回 `[]`（修复前永久阻塞） |
| **P2-5** | 取消监听器断连即死 | ✅ 已修复 | `_cancel_loop` 加重连；`test_cancel_loop_reconnects_after_connection_loss` |
| **P2-6** | 取消路径阻塞事件循环 | ✅ 已修复 | 新增 `_CancelNotifier`，`test_remote_executor_cancel_stream_is_nonblocking` |
| **P2-7** | worker 不预热 | ✅ 已修复 | `server.py:524` `prestart_agents=remote_executor is None or not self._leader_enabled` |
| **P2-8** | compose 共享卷与注释不符 | ✅ 已修复 | `docker-compose.cluster.yml` api 服务补挂 `octop_workspace`，注释改为"集群模式【必须先配 MinIO】" |
| **P3-9** | 文档互相矛盾 | 🟡 部分修复 | `production-ha.md`（RTO 改 ≤2min、"禁用 lock"改写为 `lock`+自愈设计）、`microservice-scaling-1000.md` 已改；`deployment-stage2.md` **未改** |

回归测试：`tests/unit/cluster`、`test_remote_executor.py`、`test_executor.py`、`tests/unit/gateway`
共 **482 passed**（新增 7 个针对性用例）。

### 8.2 ⚠️ 修复引入的新问题：长跑作业会被误重派（双跑）

`verify_p0_1_edge.py` 实测：当一个作业 `doing` 超过 `TURN_JOB_MAX_RUNTIME_SECONDS = 2100s`，
而**其 worker 仍然存活并在正常心跳**时，运行时检测器仍会把它 `retry_job` 回 `todo`
→ 另一个 worker 认领（实测 `[4] 另一个 worker 2 认领结果 -> (1,'doing')`），
**原 worker 也还在跑** → 同一 `job_id` 出现两条并发分片流、同一线程 checkpoint 并发写。

危害边界（已在库内确认，属于 at-least-once 的正常代价，**不是永久卡死**）：

- `procrastinate_finish_job_v1` 带 `WHERE id=job_id AND status IN ('todo','doing')`，
  旧 worker 事后 finish 不会把作业"复活/抢占"。
- `retry_job` 会恢复 `worker_id`，心跳检测器下一轮不再命中同一作业。
- 运行时检测器有 `seen` 去重，不会每 60s 反复 bump 同一作业。

**建议**：把"运行时阈值"（2100s）调大到明显高于正常回合上限，或对 `status='doing'`
且 `worker_id` 仍存活的心跳检测结果**不做**运行时兜底重派，只做"worker 行已被 prune"的那一类重派。

### 8.3 第二轮问题在本提交中的状态（多数未覆盖）

| # | 第二轮问题 | 本提交是否处理 |
|---|---|---|
| **R2-1** | 阶段 0 多副本（N 个 `octop run` + LB）无门控 | ❌ 未处理。`launch.py:74` 仍是 `OctopServer()`（`leader=True`），且 `leader_runner` 只在 `execution_mode=="remote"` 内创建（`server.py:383-399`）→ 本地模式多副本每个都内联跑全部后台单例 |
| **R2-2** | 控制面迁移无跨进程锁 | ❌ 未处理 |
| **R2-3** | `octop agent-worker --workspace` 参数不存在 | ❌ 未处理（文档也没改） |
| **R2-4** | worker 预热 | 🟡 代码已修（见 P2-7），但 `deployment-stage2.md:75` 的说明未同步 |
| **R2-5** | IM 渠道状态在集群下闪 | ❌ 未处理 |
| **R2-6** | 集群 LB 配不了健康检查 | ❌ 未处理 |
| **R2-7** | `infra/` 反向 import `api/`（分层违规） | ❌ 未处理（`remote.py:93` 仍在） |
| **R2-8** | 压测脚本硬编码绝对路径 | ❌ 未处理 |
| **R2-9** | 队列无启动预热 / 事件发布无定时 flush / 读取无 LIMIT | ❌ 未处理 |

### 8.4 第三轮小结

- **第一轮的 4 个实测缺陷（P0-1 / P1-2 / P1-3 / P1-4）全部修复且经实测确认**，配套 7 个回归测试，
  482 个相关单测全绿。修复质量高，接线收敛（`cancel_turn` / `BackgroundGate` / `_CancelNotifier`）干净。
- **新增 1 个修复引入的边界问题**：长跑作业（>2100s 且 worker 存活）会被误重派导致双跑（§8.2），
  建议调整阈值或收窄重派条件。
- **第二轮 9 项基本未动**，其中 **R2-1（阶段 0 多副本无门控）与第一轮 P1-2 同根因**，
  是"按文档推荐拓扑部署就会踩"的问题，建议优先于其余项处理。

