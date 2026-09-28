# Octop 1000+ 用户微服务式水平扩展调研（2026-09-24）

> 状态：调研完成，待评审。
> 结论先行：**代码接缝比预期有利**——agent 执行层与 HTTP 层之间已存在窄接口
> （`AgentManager.stream/call/resume_hitl/cancel`），流式 chunk 全部 JSON 可序列化，
> 且每个 chunk 已持久化到 DB（带序号）。执行层剥离为独立 worker 服务是**中等工作量**
> （核心是 1 个执行器抽象 + 5 处调用方替换 + 1 条流回传通道），不需要重写 harness。
> 分两步走：先做 500 方案的"同构轻副本"（见 `scaling-500-users.md`），再实施本文的
> "执行层剥离"——两者是同一条演进路径的前后两个阶段，不是二选一。

---

## 1. 负载模型

| 指标 | 估算 |
|------|------|
| 注册用户 | 1000+ |
| 峰值并发会话 | 100–200（按 10–20%） |
| 单会话特征 | 等 LLM 流式响应为主（IO 密集），偶发 CPU 段（工具结果处理、SSE 序列化、上传解析） |
| 任务下发速率 | 回合级（每会话一次一个 turn），非消息级——远低于队列吞吐瓶颈 |

## 2. 代码接缝调研结论（本轮核实）

| # | 事实 | 位置 | 对拆分的意义 |
|---|------|------|-------------|
| 1 | 执行入口是**窄接口**：`stream()` / `call()` / `resume_hitl()` / `cancel_stream()` / `get/set_thread_model()`，均为 `(agent_id, request)` 签名 | `infra/agents/manager.py` L1228 起 | 在接口后插入"远程执行器"即可，harness 不用动 |
| 2 | 调用方仅 **4 处**：网关主处理器、IM 流工程、cron 投递、多 agent 团队 | `gateway/process/processor.py` L1018/L1116、`gateway/process/stream_project.py` L249/L276、`cron/delivery.py` L143、`agents/teams/team_manager.py` L296 | 改造面收敛、可逐处切换 |
| 3 | 流 chunk **全部 JSON 可序列化**（SSE 层就是 `json.dumps` + model_dump 兜底） | `api/routers/chat/sse.py` | chunk 可经任何通道回传，前端协议不变 |
| 4 | **每个 chunk 已持久化**：`observe_chunk` → trajectory_event_repo（DB，带 seq），另有进程内 `TrajectoryLiveBus` 实时扇出；查询接口 `list_from_seq` 已存在 | `gateway/process/processor.py` L276、`history/trajectory/{service,live}.py` | 跨服务 SSE 可用"DB 回放 + 实时订阅"实现，不需要新存储 |
| 5 | 同线程并发互斥是**进程内 asyncio.Lock**（key = agent_id + thread_id） | `manager.py` L906 | 多服务后需分布式互斥（PG advisory lock 或队列按 thread 分区天然串行） |
| 6 | HITL 中断状态在 PG checkpoint（per-agent memory schema），恢复走 `resume_hitl` | harness_agent 包 + memory 后端 | 任意 worker 均可恢复中断会话 ✓ |
| 7 | harness_agent 是独立 wheel（`manager/memory/backends/mcp` 自成体系），Octop 通过接口调用 | site-packages | 具备独立进程运行的前提（仍需 PoC 验证 checkpoint 并发正确性） |
| 8 | workspace 已支持 cos/s3/oss/obs 对象存储；上传/附件走适配层 | `infra/backend/adapter.py` | worker 与 api 共享对象存储即可互见文件 ✓ |

## 3. 目标拓扑（服务拆分）

```
                      ┌── LB（TLS · 轮询/一致性哈希 · 健康检查）──┐
                      │                                          │
              ┌───────▼──────┐  ┌──────────┐  ┌──────────┐
              │  octop-api   │  │ octop-api│  │ octop-api │   ×N：HTTP/SSE/WS、
              │ （无执行态）  │  │          │  │          │    认证、CRUD、上传
              └───────┬──────┘  └────┬─────┘  └────┬─────┘
                      │  任务队列（回合任务：JSON payload + 回传 topic）
              ┌───────▼──────────────────────────┐
              │         octop-agent ×M            │  harness 运行时、
              │  消费回合任务 → 流式执行 agent      │  agent 懒加载、
              │  chunk → 回传通道 + DB             │  per-worker 并发上限
              └───────────────────────────────────┘
              ┌───────────────────────────────────┐
              │   octop-background ×1（值班）       │  cron / IM 入口 / 主动关怀 /
              └───────────────────────────────────┘  备份 / TLS 续期（租约或单副本）
                      │
        PostgreSQL 16 + pgvector（控制面 · memory · trajectory 事件流）
        COS / S3（workspace · 上传 · 知识库文档）
        （可选）Redis：流回传 pub/sub + 配置广播 + 限流
```

- **api 层不再加载任何 agent**：内存从 O(agent 总数) → O(1)，可任意横向扩；
- **agent 层内存 ∝ 该 worker 活跃会话数**：懒加载 + 执行完可卸载（空闲回收）；
- 扩容动作：api 不够加 api，agent 不够加 agent——微服务式的独立伸缩。

## 4. 关键机制设计

### 4.1 任务下发（api → agent）

- 任务 = `{agent_id, thread_id, request(dict), reply_channel, cancel_flag_key}`；
- `request` 即现有 dict（`InboundMessage` 等均可 JSON 化）；
- 下发时机：`processor` 处理入站消息时投递，**任务行与消息状态同一事务**（不丢单）。

### 4.2 流回传（agent → api → 浏览器 SSE）

复用既有事实（接缝 #4），两条通道按是否引入 Redis 选择：

| 方案 | 机制 | 优劣 |
|------|------|------|
| A. DB + NOTIFY（零新组件） | worker 写 trajectory 事件（已有）+ `pg_notify(thread)` 批量通知（50ms 合并 flush）；api SSE 端 = `list_from_seq` 拉取 + LISTEN 唤醒 | 零新组件、事件不丢；延迟增加 ≤50ms（token 流可接受）；DB 写入量增加（turn 级聚合可缓解） |
| B. Redis pub/sub（若已引入 Redis） | worker 逐 chunk publish 到 `turn:{thread_id}` 频道；api 订阅直通；DB 仍持久化（对账/断线回放） | 最低延迟；多一个常驻组件 |

推荐：**先 A 后 B**——A 是零组件基线，压测不达标再引 Redis（届时 Redis 同时服务配置广播与限流）。

### 4.3 并发互斥与取消

- 同 thread 串行：任务按 thread 进入队列，**同一 thread 的任务由队列保证串行**（分区键 = thread_id）——替代进程内 asyncio.Lock；
- 取消：api 置 cancel 标志（DB/Redis）+ relay 发 `cancel` 事件，worker 收到后调 `harness.cancel`；
- 断线重连：SSE 重连后先 `list_from_seq(last_seq)` 回放再接实时——`list_from_seq` 已存在，天然支持。

### 4.4 其余跨服务机制

与 500 方案（`scaling-500-users.md` §4/§10）一致，直接继承：
后台单例门控（值班租约/角色拆分）、配置热更新广播（PG NOTIFY 或 Redis）、
迁移互斥、agent 懒加载、知识库向量迁 pgvector、文件外置对象存储。

## 5. 组件选型（任务队列）

| 组件 | 现状 | 评价 |
|------|------|------|
| **Procrastinate**（PG 原生） | 活跃，LISTEN/NOTIFY + SKIP LOCKED，原生 async，任务与业务同事务 | ⭐ 首选：契合 Octop"PG 已是硬前提、最少依赖"哲学 |
| **SAQ** | 活跃（2026-01 更新），broker 支持 **PostgreSQL 或 Redis**，<5ms 延迟，带 Web UI | 次选；若后续引 Redis 可平滑换 broker |
| TaskIQ | 活跃，全 async、多 broker、类型化 | 功能最全但偏重；多 broker 需求出现时再考虑 |
| 自研 SKIP LOCKED 任务表 | —— | 可行（模式简单、社区文档充分），但重试/死信/可见超时都要自己写；Procrastinate 已封装好 |
| Celery / Dramatiq / RQ | Celery 复杂且 async 弱；Dramatiq/RQ 同步模型 | 不匹配 Octop 全 asyncio 栈，排除 |
| arq | 已停止维护 | 排除 |

队列吞吐参考：PG 任务表实测 ~2000 jobs/s（4 asyncio worker）——本场景回合级下发（≤数百/分钟）余量两个数量级以上。

## 6. 改动清单（文件级）

| 改动 | 内容 | 量级 |
|------|------|------|
| 新增 `infra/exec/` | `AgentExecutor` 接口 + `InProcessExecutor`（包装现有实现）+ `RemoteExecutor`（队列下发 + 流订阅） | 中（~500 行 + 单测） |
| 替换 4 处调用方 | processor / stream_project / cron delivery / team_manager 改走 executor 接口（行为开关：`OCTOP_EXEC_MODE=local\|remote`） | 小（每处 ~10 行） |
| `TrajectoryLiveBus` 抽象 | 接口化 + `PgNotifyBus` / `RedisBus` 实现，SSE 端不变 | 中 |
| thread 串行化 | 移除进程内 Lock 依赖，改队列分区（remote 模式）/ 保留（local 模式） | 小 |
| 新增 CLI 角色 | `octop api` / `octop agent` / `octop background`（或 env 角色），api 角色跳过 `_boot_runtime` 中 agent 全量加载 | 小 |
| chunk 批量 flush | worker 侧 50ms 合并推送 | 小 |
| 观测 | executor 层埋点（队列深度、执行延迟、回传延迟） | 小 |

不需要动的：harness_agent 包、前端、SSE 帧格式、DB schema（新增队列表 + 既有 trajectory 表复用）、workspace/对象存储层。

## 7. 实施路线

> **实施状态（2026-09-25）**：三项 PoC 全部 PASS（见 §8），阻塞项清零。
> **阶段 1 已落地**并通过验证：
>
> - 新增 `src/octop/infra/agents/executor.py`：`AgentExecutor` 协议
>   （stream / call / resume_hitl / cancel_stream，签名与 `AgentManager`
>   公有执行方法一一对应）+ `InProcessExecutor`（纯委托实现，无自有锁/缓冲）。
>   协议细节：流式方法声明为 `def … -> AsyncIterator`（异步生成器函数的调用
>   类型），实现侧为 `async def` + `yield`——否则 mypy 会把调用方判定为
>   "协程不可迭代"。
> - **接线方式（关键取舍）**：`GlobalProcessor` / `Gateway` /
>   `CronDeliveryService` / `TeamManager` 构造器与 `project_stream` /
>   `project_resume_stream` 函数新增 `executor: AgentExecutor | None = None`
>   参数，默认 `InProcessExecutor(agent_manager)`；调用点统一改走
>   `self._executor.*`。`AgentManager` 结构化满足协议（默认实现下行为与
>   原版逐字节一致，且兼容既有测试的猴补丁注入）。阶段 2 在组合根构造处
>   注入 `RemoteExecutor` 即完成切换，调用点零改动。
> - cancel 直调路径（`ws.py`、`slash/runtime_bridge.py`）保持
>   `agent_manager.cancel_stream`（结构化满足协议）；取消跨服务化的接线
>   归入阶段 2。
>
> **验证证据**：新增 `tests/unit/agents/test_executor.py` 9 例全过（协议
> 符合性、四方法参数透传、chunk 透传）；受影响套件（agents/gateway/cron/
> api/auth）**1623 通过 / 0 失败**；全量 pytest 见下表；`mypy src/octop`
> 与改动前基线完全一致（3 个存量错误，均在本次未触碰的文件：
> `db/repos/connectors.py:271` ×1、`users/manager.py` 域 ×1、
> `agents/manager.py:1490` ×1——其中 manager.py:1490 经 `git stash` 对照
> 实验证实为存量，仅因新增 import 行号位移至 1504）。ruff 对本次全部改动
> 文件零告警（`ruff check src tests` 的 10 个存量告警均在未触碰文件）。

| 阶段 | 内容 | 退出标准 |
|------|------|---------|
| **阶段 0**（即 500 方案） | PG + 对象存储 + 懒加载 + 后台门控 + 同构多副本 | 双实例稳定；此形态本身可撑到 ~500–800 活跃 |
| **阶段 1** ✅ | PoC（§8 全 PASS）+ `AgentExecutor` 抽象 + `InProcessExecutor` 落地（行为等价重构，无功能变化） | `make all` 绿；单实例行为回归通过 |
| **阶段 2** | `RemoteExecutor` + PG 队列 + DB/NOTIFY 流回传；api 角色剥离执行 | 双 api + 双 agent 拓扑下全链路聊天/工具/HITL/取消/断线重连通过；token 流延迟增量 ≤100ms |

> **阶段 2 实施状态（2026-09-25）**：核心链路已落地并通过双进程联调（机制级；
> HTTP/SSE 全链路属阶段 3）。
>
> **新增代码**：
> - `src/octop/infra/agents/remote.py`：`RemoteExecutor`（api 侧，实现阶段 1 的
>   `AgentExecutor` 协议：入队 → LISTEN+增量拉取回传 chunk）、`AgentWorker`
>   （agent 侧，procrastinate worker + 本地执行 + 50ms 批量 flush 回传，
>   PoC 2 模式）、`TurnEventPublisher` / `TurnStreamReader`、
>   `ensure_turn_schema` / `ensure_queue_schema`。事件表
>   `octop_turn_events`（seq BIGSERIAL, job_id, payload），读端带进度看门狗
>   （默认 1800s 无事件即报错，防 worker 死亡后 SSE 永久悬挂）。
> - 同 thread 串行：任务以 procrastinate `lock=octop-turn:{agent}:{thread}`
>   投递，procrastinate 保证同锁任务按创建顺序独占执行——替代进程内
>   `asyncio.Lock`（PoC 1 阶段 3 的硬性要求）。已知坑（评审 P0-1）：worker
>   崩溃后僵尸 `doing` 作业会永久占锁，`AgentWorker._stalled_loop` 已实现
>   周期 `get_stalled_jobs` + `retry_job` 自愈（心跳 60s 判定真死重派；
>   worker 假活卡住不重派，由回合运行时上限 2100s 判死释放锁，见 D15）。
> - 取消：api 侧 `pg_notify('octop_turn_cancel')`，worker 常驻 LISTEN 转发
>   本地 `cancel_stream`；只取消活跃流，与本地语义一致（排队未启动的任务
>   不中止）。
> - 组合根接线：`OctopConfig.execution_mode`（`OCTOP_EXEC_MODE=local|remote`，
>   默认 local 行为与原版逐字节一致）+ `OCTOP_QUEUE_DSN`（仅 env，不落盘）。
>   remote 时 `_boot_runtime` 向 Gateway/CronDeliveryService 注入
>   `RemoteExecutor`，且 api 节点 `registry.boot(prestart_agents=False)`
>   ——api 内存从 O(agent 总数) 降为 O(1)。
> - 新 CLI：`octop agent-worker`（boot 完整 OctopServer + 消费队列；后台
>   单例值班门控仍属阶段 0/3）。
> - 依赖：`procrastinate>=3.8,<4`。
>
> **联调证据**：`scripts/stage2/run_stage2.py` + `worker.py`（嵌入式真 PG +
> 真 procrastinate + 真 HarnessAgent + 伪 LLM，1 api 进程 + 2 agent 进程），
> 结果 `scripts/stage2/result.json`，6 场景全 PASS：
>
> | 场景 | 关键结果 |
> |------|---------|
> | 1 流式端到端 | 171 chunks 顺序到达、JSON 无损、干净收尾 |
> | 2 非流式调用 | result dict 正确回传 |
> | 3 双线程并行 | 两 worker 同时消费，双双成功 |
> | 4 同线程串行 | turn B 结束比 turn A 晚 20.5s（gap ≥250ms 判据），队列锁生效 |
> | 5 执行中取消 | 首 chunk 后 66.6ms 发起取消，71.1ms 内干净终止；worker 随后健康 |
> | 6 错误传播 | worker 侧异常以 `RemoteTurnError` 在 api 侧原样抛出 |
>
> **实施中发现的关键坑（生产必读）**：
> 1. **procrastinate schema 非幂等**（裸 `CREATE TYPE`）：多节点并发首次启动
>    会冲突。`ensure_queue_schema` 用 PG advisory lock 串行化 + 先查
>    `procrastinate_jobs` 再应用。
> 2. **懒初始化 checkpointer 与领任务事务死锁**：harness 的
>    `PostgresSaver.setup()` 含 `CREATE INDEX CONCURRENTLY`，会等待所有
>    更早开启的事务；而 procrastinate 领取任务后其 fetch 连接处于
>    "idle in transaction"，形成循环等待。`AgentWorker` 因此提供 `warmup`
>    钩子——**必须在领取任何任务之前**完成 checkpointer 初始化
>    （`octop agent-worker` 场景同理；上游 harness_memory 改为非
>    CONCURRENTLY 建索引可根治）。
> 3. **预热等待不能用阻塞式 `pg_advisory_lock`**：等锁的隐式事务同样先于
>    CONCURRENTLY 存在，会自死锁。用 `pg_try_advisory_lock` + sleep 重试。
| **阶段 3** ✅（机制级） | 后台单例选主 + PG 压力收敛 + 压测定容 + 编排（compose）+ 文档 | 40 路并发全链路 PASS（见阶段 3 实施状态）；200 并发 × 30min 待生产硬件复测 |

> **阶段 3 实施状态（2026-09-25）**：核心落地并通过真 PG 全链路压测
> （`scripts/stage3/run_stage3.py`，结果 `scripts/stage3/result.json`）。
>
> **新增/改动代码**：
> - `src/octop/infra/cluster/leader.py`：`LeaderRunner` + `LeaderHandle`——
>   会话级 advisory lock（key 918273647）选主，抢到锁启动挂接服务、会话断
>   自动停服务并重争抢（无 TTL / 无脑裂窗口，接管秒级）。`OctopServer`
>   在 remote 模式挂接 4 个后台单例：cron 调度器、IM 频道轮询
>   （Gateway 新增 `im_active` 门控）、proactive-care、知识索引恢复；
>   多副本 api 恰好一个 leader 执行。
> - 角色边界：`OctopServer(leader=False)` + `octop agent-worker`——worker
>   节点纯执行，永不竞争 leadership。
> - `remote.py` 压力收敛：`NotifyHub`（api 侧共享 LISTEN，1 连接）+
>   `TurnFetchPool`（共享拉取池，默认 8，替代每流 2 专用连接）+
>   `turn_events.created_at` 保留期清理（`prune_turn_events`，worker 周期执行）。
> - 编排：`docker/docker-compose.cluster.yml`（lb + api ×N + worker ×N +
>   pgvector + MinIO）与 `docker/cluster-lb.conf`（SSE 关缓冲）。
>
> **压测结果**（40 路 × 2 worker × 并发 8，真 HarnessAgent + 伪 LLM）：
> 吞吐 ≈ 450 流/分钟；首 chunk p50 1.26s / p95 3.25s；PG 连接峰值 47
> 且按进程归因全部有界（api 13 / worker 各 17 / hub 1）；清理与
> 选主/接管判据全 PASS。
>
> **压测发现并修复的竞态**：`RemoteExecutor._ensure_open` 无锁时，
> 冷启动并发首用会各自实例化队列/hub/拉取池（每套含 procrastinate
> 连接池），连接数瞬间打满 PG `max_connections`。已用 `asyncio.Lock`
> 串行化懒初始化，并以 `tests/unit/agents/test_remote_executor.py`
> 两个并发回归单测锁定。
>
> **待办（容量承诺级）**：200 并发会话 × 30 分钟复测需生产硬件 + 真 LLM
> （本阶段为 40 路机制级验证）；K8s 编排与 chaos 演练归运维落地项。

## 8. PoC 清单（阶段 1 前置，阻塞项）

1. **harness_agent 独立进程正确性**（最高优先级，同 500 方案 PoC 1）：两个进程先后加载同一 `agent_id`、各自执行回合，验证 PG checkpoint 读写一致、无文件锁/端点冲突（注意 `MemorySlimControl` 回环端点文件需按实例隔离——已确认按 OCTOP_HOME 隔离即可）；

   > **✅ PoC 1 已完成（2026-09-24），结论：PASS，跨进程执行成立。**
   >
   > 脚本：`scripts/poc1/`（`run_poc.py` 编排 + `worker.py` 独立进程 + `fake_llm.py` 协议级伪 LLM；结果 `scripts/poc1/result.json`）。
   > 环境：真实 harness_agent wheel + 真实嵌入式 PostgreSQL（pgserver）+ OpenAI 协议伪 LLM（服务端记录每个请求的完整消息列表，作为服务侧证据）。
   >
   > 关键机制（代码级）：`HarnessAgent._resolve_checkpointer()` 在 `memory_enabled=True` 且 postgres backend 时，**`Memory` 实例本身就是 LangGraph checkpointer**（`BaseCheckpointSaver` 子类，checkpoint 与记忆同库同事务）——跨进程共享是结构性设计而非巧合。
   >
   > 三阶段结果：
   >
   > | 阶段 | 场景 | 结果 |
   > |------|------|------|
   > | 1 跨进程接续 | 进程 A 在线程 T1 执行第 1 轮 → 进程 B 同线程执行第 2 轮 → 进程 C 读历史 | ✅ B 的 LLM 请求中**携带了 A 的消息**（服务端证据）；C 经 `aget_history` 读到 A 的标记；历史长度单调递增（10→12） |
   > | 2 双进程并发不同线程 | 同一 agent 命名空间 + 同一 workspace 目录，两进程同时在 T2/T3 执行 | ✅ 双双成功，无文件锁冲突；跨进程记忆召回（`<memory-context>`）正常注入 |
   > | 3 双进程并发同线程 | 两进程同时写同一线程 | ✅ 均完成且无损坏；但低争用下"未坏"≠"串行"，**队列按 thread 分区串行（`lock` + stalled 自愈，见 §5）为硬性要求** |
   >
   > 附带确认与注意事项：
   > - 默认路径（memory 关闭/sqlite backend）的 checkpointer 是 workspace 内 `checkpoints.sqlite`（AsyncSqliteSaver）——**跨节点共享该文件不安全**；Octop 生产配置必须保持 postgres memory backend（本就如此）。
   > - 本次 PG 走本机 unix socket；上生产前需在 TCP + 连接池的真实 DSN 上复跑一轮（预期无差异，属配置项）。

2. **流回传延迟**：DB+NOTIFY（50ms flush）vs Redis pub/sub 的 token 流延迟实测，确定通道 A 是否达标；

   > **✅ PoC 2 已完成（2026-09-24），结论：PASS，通道 A（DB+NOTIFY）达标，无需引入 Redis。**
   >
   > 脚本：`scripts/poc2/run_poc2.py`（生产者进程按真实 token 速率产出 → 批量 flush → 单事务 INSERT+pg_notify；消费者专用 LISTEN 连接事件驱动唤醒 + 独立连接按 seq 增量拉取；嵌入式真 PG）。结果：`scripts/poc2/result.json`。
   >
   > | 场景 | p50 | p95 | p99 | max | 达标(≤100ms) |
   > |------|-----|-----|-----|-----|-----|
   > | A 每 token 即时通知（机制开销上界） | 0.66ms | 0.84ms | 1.29ms | 5.03ms | ✅ |
   > | B 50ms flush @ 30 tok/s（设计默认） | 3.65ms | 36.32ms | 36.92ms | 70.94ms | ✅ |
   > | C 50ms flush @ 100 tok/s（高压） | 23.01ms | 51.34ms | 53.09ms | 53.97ms | ✅ |
   >
   > 结论：NOTIFY 唤醒→增量 SELECT 的机制开销约 1ms（场景 A 证明）；50ms 批量窗口下端到端 p95 ≈ flush 窗口 + 少量开销，远低于 100ms 目标；100 tok/s 高压下无明显退化。**50ms 批量 flush 的设计值得到实测支持；Redis 通道 B 可作为纯备选保留，不必实施。**
   > 注意事项：本实测为本机 unix socket + 单生产者单消费者；上生产前在 TCP DSN + 目标拓扑（3 worker 并发写入）下复测一轮即可（预期增量主要来自网络 RTT）。
3. **取消与断线**：执行中取消、SSE 断线重连回放的正确性（依赖 `list_from_seq` 语义验证）。

   > **✅ PoC 3 已完成（2026-09-24），结论：PASS，取消与断线回放语义均成立。**
   >
   > 脚本：`scripts/poc3/run_poc3.py` + `worker.py`（复用 PoC 1 真 agent + PoC 2 通道基建）；结果 `scripts/poc3/result.json`。
   >
   > **阶段 1 执行中取消（真实 HarnessAgent + 慢流伪 LLM）**：
   > - 流进行至 ~213ms（15 chunks / 全程约 6s）时调用 `agent.cancel(thread)` → **流即时干净终止**（elapsed 213.5ms，无异常），worker 进程 rc=0 干净退出（aclose 无悬挂连接）；
   > - 另一进程随后读该线程历史：可读、含用户消息、无损坏（历史长度 1——被取消回合只落用户消息，未写入残缺 assistant 回合；用户重发一条消息即可继续，与 HA 文档承诺一致）。
   >
   > **阶段 2 断线重连回放（通道级，模拟 api 节点死亡两次）**：
   > - 120 token + END 共 121 行，两次中断（seq=7 后死亡、seq=23 后死亡），新消费者按 `WHERE seq > last_seq ORDER BY seq` 续传；
   > - 结果：**121/121 全收、无缺口、无重复**（exactly-once），且每次恢复起点恰为 `last_seq + 1`——`Last-Event-ID` 语义对 `list_from_seq` 的依赖得到实证。

## 9. 风险登记

| 风险 | 等级 | 缓解 |
|------|------|------|
| ~~harness checkpoint 不支持跨进程并发/交替访问~~ | ~~高（阻塞）~~ → **已解除**（PoC 1 PASS，2026-09-24，见 §8）：跨进程接续、双进程并发不同线程均验证通过；同线程并发仍需队列锁串行（已实现：`lock` + `_stalled_loop` 僵尸自愈） |
| 浏览器会话（`api/routers/browser`、harness_browser）是节点内有状态资源 | 中 | agent 用到浏览器时粘性路由到持有会话的节点；或将浏览器会话外置为独立服务 |
| DB+NOTIFY 回传在高频 token 下 DB 写放大 | 低（已实测缓解） | PoC 2 PASS（2026-09-24，见 §8）：50ms flush @ 100 tok/s 下 p95=51ms，无退化；50ms 批量天然控制写入频率，多 worker 并发写入场景上生产前复测即可 |
| 团队（multi-agent）场景一次消息级联多个 `stream()` | 中 | 级联发生在 worker 内部（team_manager 在 agent 侧执行），无需跨服务编排——已按调用方位置确认 |
| 队列任务可见超时与长回合冲突（单回合可能 >10min） | 低 | 心跳续锁（Procrastinate 原生支持）+ 任务幂等（投递状态在 repo） |

## 10. 与 500 方案的关系

`scaling-500-users.md` 的路线 B（同构轻副本 + 值班租约）是本方案的**阶段 0 与公共底座**：
PG/对象存储/懒加载/门控/广播五项改造完全复用。本文新增的只有"执行器抽象 + 队列 + 流回传"
三件事。因此无论最终是否走到 1000+，先实施 500 方案都不会产生返工。
