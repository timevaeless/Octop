# Octop 生产级高可用方案（可靠性审计，2026-09-24）

> 定位：对 `scaling-500-users.md`（同构轻副本）与 `microservice-scaling-1000.md`（执行剥离）
> 的高可用部分做**逐组件故障模式审计**，每一项给出：故障场景 → 检测方式 → 恢复机制 →
> RTO → 数据损失 → 依据（代码位置或组件语义）。不做无依据的承诺。
> 范围假设：**单区域、多可用区**；跨区域容灾不在本期。

## 0. 可靠性目标

| 指标 | 目标 | 说明 |
|------|------|------|
| 可用性 | 99.9%（月停机 ≤ 43 min） | 计划内滚动升级不计入，且不中断在途会话（见 §2.9） |
| 会话数据 | RPO = 0 | 会话/轨迹/chunk 全部落 PG（含 checkpoint），无内存态依赖 |
| cron 任务 | 最坏错过 1 个触发周期 | 见 §2.3，配套 reconcile 补跑改造 |
| 检测依赖 | 不依赖节点时钟 | 选主用 PG 会话级 advisory lock（DB 仲裁），不用 TTL 时钟租约 |

## 1. 组件与故障模式总表

| 组件 | 副本数 | 故障检测 | 恢复动作 | RTO | 数据损失 |
|------|--------|---------|---------|-----|---------|
| octop-api | ≥2 | LB 健康检查（建议 5s×2 失败摘除） | LB 摘除；客户端 SSE 自动重连 | 10–30s | **0**（SSE 按 `Last-Event-ID` 回放） |
| octop-agent | ≥2 | Procrastinate 心跳 / stalled 判定 + `_stalled_loop` 重试扫描（60s） | `retry_job` 重派任务；LangGraph 从 checkpoint 续跑 | ≤2min | 0（步骤级 checkpoint；语义 at-least-once） |
| octop-background | 1+standby | PG advisory lock 竞争（Follower 轮询 5s） | Follower 获锁接管；重连 IM 渠道 | 15–30s | 0；cron 最坏错过 1 周期（见 §2.3） |
| PostgreSQL | 1主1备+ | 托管 PG 自动探测 / Patroni（etcd 仲裁） | 自动主备切换 | 托管 30–120s；Patroni 10–30s | 同步复制 RPO=0（性能 −10~20%）；异步复制有复制延迟窗口 |
| 对象存储 | 托管 | — | — | — | COS/S3 11×9 持久性；自建 MinIO 需 ≥4 节点纠删码 |
| LB | 2 | — | keepalived VIP / 云 LB 自带 | <5s | 0 |
| Redis（可选） | 1主2从+Sentinel | Sentinel | 自动提升 | 10–30s | pub/sub 消息可能丢 → 回放兜底（见 §2.7） |

## 2. 关键故障场景逐项审计

### 2.1 api 节点崩溃（含滚动升级）

- **trajectory SSE 无损恢复（代码级保障）**：trajectory SSE 端点支持 `Last-Event-ID` 头与
  `after_seq` 参数，重连后从 DB 按序回放，并对重放竞态做旧帧去重
  （`api/routers/chat/trajectory.py` L144–224）。api 重启期间 worker 照常执行、chunk 照常
  落 DB → 轨迹台账零丢失。
- **主聊天流在途续接（cluster plan D3，已实现）**：worker 认领回合时把 `thread → job_id`
  落库（`octop_turn_active_jobs`，完成/失败即删）；任意 api 节点经
  `RemoteExecutor.active_job_id()` 发现该 job 并 `attach()` 重开
  `TurnStreamReader`——从 seq 0 回放已持久化事件 + 共享 NotifyHub 跨节点唤醒续流。
  续播入口：dashboard WS `subscribe`（自动：hub 空闲时查映射并转播，
  `api/routers/chat/ws.py` `_maybe_relay_remote_turn`）与
  `GET /agents/{id}/threads/{id}/turn/stream`（SSE，供非 WS 客户端）。
  **边界**：续接为裸 chunk 流；崩溃前由原读端写入的 `thread_messages` 投影与轨迹
  不由续播端重写（客户端按 chunk 类型幂等渲染）。投影级续接列为后续工作。
- 在途 HTTP 请求失败由客户端重试；turn 任务在队列中不受影响。主聊天流续接：
  worker 认领时落库 thread→job 映射，任意 api 节点经 WS subscribe 转播或
  `GET /api/agents/{id}/threads/{id}/turn/stream`（SSE）回放 + 跨节点唤醒续流。
- readiness 已实现（`/api/health/ready`，无鉴权，D9）：DB 连接 + 迁移版本匹配才 200，
  否则 503；`docker/cluster-lb.conf` 的 upstream（resolve+max_fails）与 compose
  api healthcheck 以此为准，可区分"活着"与"能服务"。

### 2.2 agent worker 崩溃（执行中回合）

- **检测**：Procrastinate worker 每 10s 上报心跳（`procrastinate_workers` 表）；心跳超 30s
  判定 stalled（组件默认值，可配）。
- **恢复**：`get_stalled_jobs()` + `retry_job()` 重派（**必须自建周期任务**，组件不自动做，
  生产清单见 §4）；worker 重启时也会 `prune_stalled_workers` 清理。
- **正确性**：LangGraph checkpoint 步骤级落 PG → 重跑从断点继续；at-least-once 语义下的
  重复帧由 thread 串行 + 客户端 seq 去重吸收。
- **同 thread 并发安全**：任务以 procrastinate `lock = octop-turn:{agent}:{thread}` 投递
  （组件语义：同锁仅一个任务可处于 doing，后续排队串行）。**已知坑与自愈（已实现）**：
  worker 崩溃时组件只清 worker 行、不会把 `doing` 作业改回 todo——僵尸作业会永久占锁。
  `AgentWorker._stalled_loop` 周期（60s）执行 `get_stalled_jobs()`，把 **worker 真死**
  （心跳停跳 / 行被清理）的作业 `retry_job()` 重派回 todo，procrastinate 重新执行
  （checkpoint 断点续跑）。
  **worker 假活**（存活 + 心跳新鲜 + 回合卡住，如 LLM 上游 hang / 工具不返回）**不重派**
  （避免双跑）：由 `_execute_turn` 的**回合运行时上限**兜底（cluster 评审 D15，
  `TURN_JOB_MAX_RUNTIME_SECONDS = 2100s`）——超时判死，发错误哨兵给读者，作业标记
  失败 → 锁释放，线程恢复可用（不做 checkpoint 重试，语义为 fail-fast）。
- RTO = stalled 判定 60s + 重试扫描周期 60s ≈ **≤2min**（评审 P0-1 修复后；
  此前文档承诺的 90s 依赖未实现的 retry 周期，已修正为实测实现值）。
  假活卡死的 RTO = 运行时上限 2100s（下限保护，防双跑优先于恢复速度）。

### 2.3 background 值班节点崩溃（cron / IM / 备份 / TLS 续期）

- **选主机制（消除脑裂）**：用 **PG 会话级 advisory lock**（`pg_try_advisory_lock`，锁绑定
  DB 连接）而非 TTL 时钟租约——进程死亡 ⇒ 连接断 ⇒ 锁由 DB 立即释放，Follower 下一次轮询
  （5s）即接管。**无时钟依赖、无双主窗口**（DB 是唯一仲裁者）。残余风险是 TCP 半开连接
  （对端假活）：对持锁连接开启 TCP keepalive + 每 10s 执行 `SELECT 1` 心跳，失败即主动退位。
- **cron 断档**：现有任务 `misfire_grace_time=60/300`（`cron/manager.py` L260/L303）——
  60s 内完成接管则不错过；更长的故障最坏错过 1 个触发周期。
  **补跑改造（必做，小）**：接管成功后 reconcile——扫描 cron 仓库中 `next_run < now` 且
  无执行记录的任务补触发一次（投递有 repo 状态，天然幂等）。
- **IM 长连接**：接管后 `reload_channels_from_db()` 重连（机制已存在）；webhook 类渠道由
  对端重试投递兜底。
- RTO ≈ 轮询间隔 5s + 渠道重连 ≈ **15–30s**。

### 2.4 PostgreSQL 主库故障（最大单点）

- **推荐托管**（腾讯云 TDSQL-C/云数据库 PG 等）：自动主备切换 RTO 30–120s，同步复制
  RPO=0，内置 PITR（WAL 归档）。
- 自建则 **Patroni + etcd**（RTO 10–30s）+ pgBackRest WAL 归档。现有 `pg_dump` 系统备份
  （`infra/backup/pg_dump.py`，`-Fc` 格式）是逻辑备份，RPO = 备份间隔，**只作二级手段**，
  不能替代 PITR——明确写入运维要求。
- 切换期间行为：api 返回 5xx（LB 重试其他实例无效则报错，客户端重试）；agent 心跳停更
  可能触发 stalled 重试——at-least-once + checkpoint 保证不产生错误结果；PG 恢复后队列
  自动继续。连接池需配置快速失败 + 指数退避重连（现有 `SqlitePool`/psycopg pool 需按此
  调参，列入实施清单）。
- **主备切换瞬间的双写**（旧主短暂接受写入）由托管 PG 的 fencing 机制保证；自建 Patroni
  使用 `pg_ctl promote` + 原 master 置为 paused，同样隔离。

### 2.5 知识库向量索引（多实例下的硬风险）

SQLite sidecar（每 KB 一个 `index.sqlite`）在多实例读 + 共享盘场景**不可靠**
（SQLite over NFS 文件锁问题）。生产 HA 要求：**向量检索必须迁 pgvector**
（控制面已支持 pgvector），文档原件走对象存储。此项为 HA 前置，不是可选项。

### 2.6 滚动升级（计划内，零中断）

1. 迁移遵守 expand-contract（先加后删，旧版本可运行于新 schema）；
2. api 逐台摘流量 → 等待 SSE 排空（客户端自动重连到其他实例）→ 替换；
3. agent worker：SIGTERM → Procrastinate 优雅停机（`shutdown_graceful_timeout` 设
   15min，等待在跑回合完成；超时 abort → checkpoint 留存 → 新 worker 续跑）；
4. background：新实例先起并尝试获锁，旧 Leader 主动 `pg_advisory_unlock` 交棒（优雅路径
   秒级切换）；失败走 §2.3 崩溃路径；
5. 金丝雀：先替换 1 台观察 30min（错误率、回传延迟）再全量。

### 2.7 Redis 故障（若采用通道 B）

pub/sub 消息不落盘，Sentinel 切换期间事件丢失——**回放兜底**：chunk 本就落 DB，
api SSE 以 `last_seq` 为准，Redis 仅作低延迟提示通道；Redis 不可用时自动退化为
DB 轮询（500ms）模式。**Redis 永远不是正确性依赖，只是延迟优化。**

### 2.8 脑裂专项结论

| 角色 | 双主可能性 | 机制 |
|------|-----------|------|
| background | 不可能 | advisory lock 由 DB 仲裁，同一时刻至多一个会话持锁 |
| agent 执行 | 不可能 | SKIP LOCKED 认领，一个任务只被一个 worker 持有；同 thread 串行靠队列 `lock`（`queueing_lock` 是去重会丢消息，禁用，见 §3 第 8 项） |
| api 写入 | 不可能（正常切换） | 依赖托管 PG fencing；自建由 Patroni 隔离旧主 |

### 2.9 容量冗余原则

N+1：api/agent 各至少 +1 冗余副本；单副本损失后余量仍承载峰值（100–200 并发会话 →
api ≥3、agent ≥3 起）。LB 双节点或云 LB。

## 3. RTO/RPO 承诺汇总

| 故障 | RTO | RPO |
|------|-----|-----|
| api 节点 | ≤30s（自动，LB 摘除） | 0 |
| agent worker | ≤90s（自动，任务重派） | 0（checkpoint 续跑） |
| background | ≤30s（自动，锁接管） | 0；cron 最坏 1 周期 + reconcile 补跑 |
| PG 主库 | 30–120s（托管自动） | 0（同步复制） |
| 整可用区 | 人工/编排恢复，小时级 | 0（数据均在另一 AZ 的托管服务内） |

## 4. 生产必做改造清单（缺一不可）

> **阶段 3 落地更新（2026-09-25）**：第 3 项已随选主落地（leader 接管 =
> cron 调度器重启，附着 misfire 补跑语义）；第 5 项已落地
> （`LeaderRunner._watch_session` 持锁连接周期 `SELECT 1` 心跳，会话死
> 锁自动释放）。`turn_events` 保留期清理已由 `AgentWorker` 周期执行
> （`prune_turn_events`）；**第 2 项 `retry_stalled_jobs` 周期任务已内置于
> `AgentWorker._stalled_loop`**（无需外配周期任务），worker 假活卡死由
> 回合运行时上限兜底（D15）。其余为配置/运维项，状态不变。
> 机制与压测证据见 `docs/deployment-stage3.md`。

| # | 项 | 类型 | 依据 |
|---|-----|------|------|
| 1 | readiness 探针（DB + 迁移版本） | 代码（小） | ✅ 已落地：`/api/health/ready`（D9），LB/compose healthcheck 已接线 |
| 2 | `retry_stalled_jobs` 周期任务（1min） | 代码（小） | ✅ 已落地：内置于 `_stalled_loop`（真死重派）；假活由 D15 运行时上限兜底 |
| 3 | background 接管后 cron reconcile 补跑 | 代码（小） | misfire_grace_time=60 的补丁 |
| 4 | 知识库向量迁 pgvector + 文档走对象存储 | 代码（中） | SQLite sidecar 多实例不可靠 |
| 5 | 持锁连接 TCP keepalive + `SELECT 1` 心跳 | 代码（小） | 防 TCP 半开假活 |
| 6 | PG 连接池快速失败 + 退避重连参数 | 配置 | 主备切换期间行为 |
| 7 | WAL 归档 PITR（托管 PG 自带 / 自建 pgBackRest） | 运维 | pg_dump 不满足 RPO |
| 8 | 队列锁规范：同 thread 串行用 `lock`；**禁用 `queueing_lock`** | 代码规范 | `queueing_lock` 是去重（同锁有作业时拒绝入队），用在用户消息上会**丢消息**；`lock` 才是串行。曾建议反置（LeanIX 案例），经 assessment §6 复核为错，已纠偏（D10） |
| 9 | 监控告警：队列深度、stalled 计数、接管事件、回放延迟、SSE 断连率 | 运维 | METRICS 为进程内计数器，需聚合（Prometheus 每实例抓取即可） |
| 10 | JWT secret 轮换窗口说明（全量登出） | 流程 | `rotate-jwt-secret` 语义，安排低峰执行 |
| 11 | chaos 演练：杀 api / 杀 agent / 杀 background / 主备切换，各季度一次 | 流程 | 验证上表 RTO 声明 |

## 5. 结论

- **已有可靠基础（代码/组件证据）**：SSE 断线回放、chunk 全落库、HITL checkpoint、
  队列心跳与停滞恢复、advisory lock 无脑裂选主、workspace 对象存储；
- **可靠性缺口共 11 项**（§4），其中 7 项为小改造、2 项中改造（pgvector 迁移、监控聚合）、
  2 项纯运维；全部有明确实现路径，无"待定"项；
- 本方案与前两份文档合并构成完整生产方案：500 文档 = 阶段 0 底座，1000 文档 = 服务拆分，
  本文档 = 可靠性审计与运维要求。

## 6. FAQ：集群下用户数据是否无感、重启会不会丢工作区

| 数据 | 存储位置 | 集群下无感？ |
|------|---------|-------------|
| 会话历史 / 登录态 / agent 记忆 | PG（控制面 + `agent_<id>` schema） | ✅ 完全无感 |
| 聊天附件 / 上传 / agent 工作区文件 | `BackendWorkspace` 适配层 | 配 COS/S3 后无感（**集群模式硬前提**）；不配则文件只在接单节点可见 |
| 知识库文档 + 向量索引 | 本地盘（无存储抽象） | ❌ 上线前必须迁 pgvector + 对象存储（§4 第 4 项） |
| 插件 / 技能包 / 备份 | 共享卷 | 低频写，共享卷即可 |

要点：重启不删数据；"工作区不见"实为"只存在于接单节点"——对象存储是解药；
`OCTOP_HOME` 各节点独立，禁止整目录 NFS；在途回合中断由 SSE `Last-Event-ID`
回放（api 重启，零丢失）或 checkpoint 续跑（worker 重启，重发一条消息继续）兜底。
