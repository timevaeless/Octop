# Octop 500+ 用户多实例扩展方案（可行性已验证）

> 状态：调研完成，待评审开工
> 结论先行：**推荐"多完整实例 + Leader 租约"路线**——不改 uvicorn 进程模型、不拆 OctopServer，
> 核心新增代码只有一个 LeaderLease 模块 + 若干 boot 门控点；最大风险点是
> **AgentManager 全量预启动 agent**，需要改为懒加载（已确认存在按需启动路径）。

---

## 1. 背景与目标

单实例 Octop（FastAPI + 内嵌 harness-agent 运行时）需支撑 500+ 注册用户。
按 10–20% 峰值并发估算，目标为 **50–100 并发会话、多实例横向扩展、可滚动升级**。

## 2. 单实例硬约束的代码证据

| # | 事实 | 代码位置 |
|---|------|---------|
| 1 | `octop run --workers N` 无效：app 以**对象**而非 import string 传给 uvicorn，workers>1 被忽略；TLS 双监听亦强制 `workers=1` | `src/octop/launch.py`（`run_foreground`、`build_app(srv)`） |
| 2 | 后台单例全部随 `OctopServer.start()` 在每个进程内启动：Gateway IM 长连接、CronManager（APScheduler 进程内调度，且自动注册 TLS 续期 + 自动备份任务）、ProactiveCareScheduler、knowledge `resume_pending_index_jobs` | `src/octop/infra/server.py` `_boot_runtime()`（L362–495） |
| 3 | AgentManager **全量预启动**：boot 时遍历所有 agent 行，非 stopped 状态逐一 `_start_agent`，每实例都加载全部 HarnessAgent | `src/octop/infra/agents/manager.py` `boot()`（L471–495） |
| 4 | JWT secret 首启自动生成于 `~/.octop/secrets/jwt_secret` | `server.py` `_ensure_jwt_secret()` |
| 5 | MemorySlimControl 向 `OCTOP_HOME` 写入含 port/pid 的本机回环端点文件 | `src/octop/infra/agents/memory_slim_control.py` |
| 6 | 启动即跑 `run_migrations`，无跨实例互斥 | `server.py` `start()` / `bind_control_plane()` |
| 7 | cron 触发器为 APScheduler 进程内 trigger（cron/interval/date），无分布式锁 | `src/octop/infra/cron/trigger.py`、`manager.py` |

**对扩展友好的既有设计**：PG + pgvector 后端完备；JWT 无状态（LB 无需粘性）；
workspace 支持 cos/s3/oss/obs 远程对象存储（`infra/backend/adapter.py`）；
SSE/流式为单请求生命周期（TrajectoryLiveBus 请求级，无跨实例推送需求）。

## 3. 路线对比

### 路线 A：uvicorn 多 worker 进程内拆分（不推荐）

把 OctopServer 拆成"无状态 API 部分"（随 worker 启动）与"后台单例部分"（独立进程）。
app 改 import string + factory。**否决理由**：OctopServer 是一体式组合根
（agent 运行时、gateway、cron 互相引用，`registry.set_cron_manager/set_team_processor` 交叉接线），
拆分等于重构组合根；且 worker 内每进程仍全量加载 agent，内存问题不变。侵入大、验证难、返工风险高。

### 路线 B：多完整实例 + Leader 租约（推荐）✅

> **2026-09-26 注记**：本路线的选主部分已由 stage3 落地（`infra/cluster/leader.py`，
> 会话级 advisory lock，取代本节设想的 lease 表——见 `docs/deployment-stage3.md`）。
> 阶段 0（local 模式多副本，无 PG）由 D4 补齐门控：`OCTOP_LEADER=0` 的副本是纯 HTTP
> 节点，不跑任何后台单例；leader 副本保持默认。仍以 stage3（remote）为主形态。

保持 `octop run` 单进程模型不变，跑 N 个完整实例（容器）+ LB。HTTP 服务所有实例均等承担；
**后台单例（cron/gateway IM 连接/主动关怀/备份/知识索引恢复/TLS 续期）只在 Leader 上运行**。

新增核心代码：

```
src/octop/infra/cluster/lease.py   # 新模块，约 200–300 行
  - PG lease 表（holder_id + heartbeat_at + ttl）或 pg_try_advisory_lock 二选一
  - LeaseRenewer：async 后台任务，心跳续租，失联自动退位
  - is_leader() 快照 + 变更事件，供门控点订阅
```

门控方式：`_boot_runtime()` 中给单例启动加 `if await lease.hold("background"): ...`，
Leader 丢失时停止调度器/断开 IM 连接；非 Leader 持续竞争，Leader 宕机后 1–2 个 TTL 内自动接管。
**该路线不动现有类结构，diff 可控、可灰度（先 2 实例验证再扩 N）**。

> 协调层（选主/互斥）也可整体替换为成熟组件实现，见 §10 组件调研结论。

## 4. 逐项可行性核查（路线 B）

| 改造点 | 结论 | 说明 |
|--------|------|------|
| LeaderLease 模块 | ✅ 可行 | 仅 PG 前提（SQLite 多实例本就不支持）；用租约表方案可读性更好，advisory lock 需持连接、增加连接池压力 |
| `_boot_runtime()` 门控 | ✅ 低风险 | 单处改动，包住 Gateway.boot / CronManager.boot / proactive start_all / resume_pending_index_jobs；TLS 续期与自动备份已注册进 cron，随 cron 一起被门控 |
| **AgentManager 懒加载** | ⚠️ 本方案最大改造 | 500 用户 × 多 agent，每实例全量预启动不可行。改为 boot 时不 `_start_agent`，首次使用（HTTP 聊天 / cron 触发 / IM 消息）按需 start。**已确认存在按需启动语义**（`start(agent_id)` 公有方法、stopped 状态可再启动），改造集中在三处调用入口 + cron 触发前的 ensure_started。需 PoC 验证 harness 跨进程加载同一 agent 的正确性 |
| JWT secret 跨实例一致 | ✅ 已解决（无需改造） | 核查发现 secret 实际存 DB `secrets` 表（`server.py` `_ensure_jwt_secret()` → `secret_repo.get_or_create("jwt", …)`），共享 PG 即天然一致；`docs/configuration.md` 中"文件 `secrets/jwt_secret`"的描述已过时（已更正） |
| 迁移并发竞争 | ✅ 简单 | `run_migrations` 外包 LeaderLease 或 PG advisory lock；滚动升级时 Leader 先迁完，其余实例起才开始服务（K8s rolling 天然满足） |
| 配置/agent 热更新跨实例 | ⚠️ 需设计 | Admin 在实例 A 改 agent/provider 设置，实例 B 的 AgentManager 不会感知（hot-reload 是进程内调用）。方案：PG `NOTIFY/LISTEN` 广播 reload 事件（psycopg 原生支持），B 收到后调 `reload_all()`；兜底为轮询 config 版本号（30s）。**注意**：聊天类请求参数（provider key 等）多数每次执行时从 DB 读，实际脏窗口影响面需在 PoC 中确认 |
| IM 渠道（Gateway） | ✅ 可行 | 非 Leader 不 `gateway.boot()` 连接渠道；IM 消息由 Leader 执行 agent（cron 同理）。HTTP 侧聊天与 IM 无耦合 |
| 文件状态 | ✅ 有前提 | 上传/知识库文档：需确认落盘路径。workspace 走对象存储 backend 已支持；其余（`~/.octop` 下 attachments/logs/plugins）**每个实例独立本地盘**——控制面上传类附件若落 `OCTOP_HOME` 本地盘则 LB 后不一致，需在实现时逐路径核对（`PathLayout`），必要时同样转对象存储。这是**工作量估算中最不确定的一项** |
| MemorySlimControl / 本机回环文件 | ✅ 无需改 | 本就设计为单机回环，实例各自本地 OCTOP_HOME 互不干扰 |
| 首次初始化 / setup 向导 | ✅ 运维约束 | 扩容前必须先由单实例完成 init + 向导 + 迁移；文档写明即可 |
| TLS / ACME | ✅ 运维约束 | 多实例下 80/443 归 LB/反代（nginx 终结 TLS），Octop 侧关闭内置 ACME；证书续期由反代层负责 |
| METRICS | ✅ 无需改 | 进程内计数器，各实例独立暴露 `/metrics` 式端点或接现有观测即可 |

## 5. 目标部署拓扑

```
            ┌── LB / nginx（TLS 终结、轮询、健康检查 /api/health）──┐
            │                                                      │
      ┌─────▼─────┐  ┌───────────┐  ┌───────────┐
      │ octop #1  │  │ octop #2  │  │ octop #N  │   每个 = 完整 octop run 容器
      │ (Leader)  │  │ (Follower)│  │ (Follower)│   本地盘仅日志/回环文件
      └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
            └───────┬──────┴──────┬───────┘
              PostgreSQL 16 + pgvector（控制面 + memory）
              COS / S3（agent workspace、上传、知识库文档）
```

- Leader 竞争对所有实例公平（租约 TTL 过期自动漂移），不指定固定主机。
- 实例数建议从 2 起步验证，按 CPU 段（SSE 序列化/工具处理）压测结果扩容。

## 6. P0 核查结果（已完成，2026-09-24）

### P0-3 文件路径审计

| 路径 | 写入方 | 多实例处置 |
|------|--------|-----------|
| `logs/`、`config.json`、`connector-cli/`、`memory-slim-control.json`、`ssl/`、`expert_market/` | 仅本实例 | 留本地盘 ✓ |
| `agents/<id>/`（含聊天附件 `inbound/`、`.env`、memory.sqlite） | 经 `BackendWorkspace` 适配层（`api/common/attachments.py` → `write_inbound(workspace,…)`） | 配置 cos/s3/oss/obs 后自动外置，**无需改代码** ✓ |
| **`knowledge/<kb>/docs/` + `index.sqlite`** | `infra/knowledge/files.py` 直接 `write_bytes`；`index.py` 为每 KB 一个 **SQLite 向量 sidecar**（L30） | ⚠️ **唯一代码级硬阻塞**。过渡：共享卷（NFS）；正解：向量索引迁 pgvector（控制面已支持）+ 文档走对象存储，列为后续项 |
| `skill-packages/`、`published_experts/`、`plugins/`、`security/tool_guard/`、`backups/` | 控制台低频写、多实例读 | 共享卷即可 |

### P0-2 进程内缓存盘点

| 缓存点 | 刷新机制 | 多实例脏窗口 |
|--------|---------|-------------|
| Agent/Provider harness 配置（`AgentManager.boot` → `providers.build_harness_configs()`） | `reload_all()` 等进程内 API | ⚠️ 需跨实例广播（PG LISTEN/NOTIFY 优先）；每次 execute 是否重读 DB 待运行时确认 |
| 验证码快照（`captcha.boot_from_services` ← env snapshot） | 重启生效（文档已注明） | 可接受 |
| 进程 env（`apply_env_file`）、`configure_browser_idle_timeout` | 重启生效 | 可接受 |
| Gateway media backends、PluginManager 已装插件、HITL policies | boot 快照 / 进程内 API 触发 | 并入 reload 广播覆盖范围 |
| JWT secret | DB `secrets` 表 | 无脏窗口 ✓ |

### 剩余 PoC（需联调环境，纯代码分析无法替代）

1. harness 跨进程访问同一 agent workspace 的并发正确性（阻塞懒加载）；
2. reload 广播覆盖面：实例 A 修改配置后，实例 B 聊天行为的实际脏窗口；
3. 单实例 100 并发压测基线。

## 7. 实施计划

| 阶段 | 内容 | 验证标准 |
|------|------|---------|
| P0 | 上述 4 项 PoC | PoC 1/2 通过；第 3 项产出路径清单；第 4 项出基线报告 |
| P1 | `infra/cluster/lease.py` + `_boot_runtime` 门控 + 迁移锁 + `OCTOP_JWT_SECRET` | 双实例起停 Leader，cron 只触发一次、IM 只连一次、备份不重复 |
| P2 | AgentManager 懒加载 +（视 PoC 2）PG NOTIFY reload 广播 | 双实例下任意实例收发聊天、admin 改配置后另一实例 ≤30s 生效 |
| P3 | docker-compose 多实例编排 + 部署文档 + 压测报告 | 2 实例滚动升级不中断在途会话；`make all` 绿 |

## 8. 明确不做（本期）

- 路线 A 的进程内拆分与 uvicorn workers 启用；
- agent 执行剥离独立 worker 池（第三阶段架构，500→1000+ 或人均重度使用再评估）；
- SQLite 多实例支持（无意义）。

## 9. 风险登记

| 风险 | 等级 | 缓解 |
|------|------|------|
| harness 不支持跨进程并发访问同一 agent workspace | 高（阻塞懒加载） | PoC 1 前置；备选方案为 agent 维度粘性路由（按 agent_id 哈希到实例） |
| 上传类文件路径核对后工作量超预期 | 中 | P0 提前暴露；最坏情况临时用共享卷（NFS）过渡 |
| Leader 漂移瞬间的 cron 重复触发 | 低 | 租约获取后延迟 jitter 再注册任务；任务本身幂等（投递有 repo 状态） |
| 配置热更新广播遗漏某类缓存 | 中 | PoC 2 盘点进程内快照点；广播失败可回退"重启实例"兜底 |

## 10. 借助成熟组件实现高可用（调研结论，2026-09-24）

**先说清组件的边界**：组件只能替代"协调层"（选主、互斥、广播）——这是自研 lease 模块
争议最大的部分；而 **AgentManager 懒加载、知识库 SQLite sidecar、harness 跨进程正确性
（PoC 1）与选不选组件无关，任何路线都必须做**。自研租约的机制（租约表 + 心跳续租 +
过期接管）与成熟组件的内部实现是同一套，组件的价值在于把实现细节交给经过大规模
生产验证的库，而非发明新机制。

### 10.1 组件选型对比

| 组件 | 替代什么 | 新增运维负担 | 适用环境 | 结论 |
|------|---------|-------------|---------|------|
| **PG 租约 / advisory lock**（无新组件） | 选主 + 迁移互斥 | 零（PG 本就是多实例硬前提） | 全部 | 自研方案所用机制；可靠性靠测试覆盖保证 |
| **Kubernetes Lease API**（`coordination.k8s.io/v1`） | 选主全流程（获取/续租/降级回调/参数关系校验） | 零（K8s 自带，无新服务） | 仅 K8s 部署 | ⭐ 最成熟方案。Python 官方客户端与 `kubernetes_asyncio` 均内置 leaderelection（`LeaseLock`），asyncio 原生回调与 Octop 事件循环同模型，替换自研 lease 后核心代码约 50 行 |
| **Redis**（`redis-py` Lock + Sentinel） | 选主 + 配置广播（pub/sub 替代 PG NOTIFY）+ 限流 | **新增常驻服务**（含 HA 需 Sentinel 3 节点） | 全部 | 单主 + Sentinel 即可（选主场景无需 Redlock/多主）；一个组件顺带解决配置广播，但违背 Octop "最少依赖"的产品原则，NAS/小团队部署成本上升 |
| etcd / ZooKeeper | 选主 | 新增重服务（etcd 至少 3 节点） | 已有集群才用 | 能力过剩，不推荐 |
| **APScheduler 4.x** | 整个 CronManager 的分布式协调层 | 零（PG data store + PG/Redis event broker） | 全部 | 4.x 内建租约式分布式调度（schedule/job lease 获取、角色分离 scheduler/worker、事件广播），机制与需求完全匹配；**但 4.x 仍是 alpha**（稳定版 3.11.3 无分布式能力），不宜上生产，列为跟踪项 |
| **角色拆分**（编排器即"组件"） | 免选主：api 角色跑 N 副本 + background 角色跑 1 副本 | 零 | compose / K8s / systemd | 无选举代码（仅需 ~20 行 env 门控跳过后台单例启动）；background 角色 HA = 编排器秒级重启恢复（非零中断接管），对 cron/IM/备份场景通常可接受 |

### 10.2 分环境推荐

| 部署环境 | 推荐协调层 | 理由 |
|---------|-----------|------|
| **Kubernetes** | 路线 B 门控点不变，选主换 **K8s Lease leaderelection** | 零新增组件 + 最成熟实现；K8s 还顺带解决滚动升级、HPA、PG/对象存储的 HA |
| **docker-compose / 裸机 / NAS**（Octop 主流自托管场景） | ① 角色拆分（最简）；② 保持 PG 租约（要求零中断 cron 接管时） | 目标用户多无 K8s；引入 Redis/etcd 与产品"单 wheel 免中间件"定位冲突 |
| 未来重度调度需求 | 跟踪 APScheduler 4 GA | GA 后可整体替换 CronManager 协调层，删自研代码 |

### 10.3 组件解决不了、仍需自研的部分（与路线 B 相同）

1. AgentManager 懒加载（每实例全量预启动 agent，任何协调组件都不管这件事）；
2. 知识库文档 + SQLite 向量 sidecar 外置（pgvector 化或共享卷，SQLite over NFS 有文件锁风险需评估）；
3. PoC 1：harness 跨进程访问同一 agent workspace 的正确性；
4. 配置热更新广播的覆盖面盘点（若用 K8s Lease 选主，广播仍建议 PG NOTIFY——不加新组件）。

## 11. 水平扩展的本质：让实例变轻（2026-09-24 补充）

回应"要微服务式扩展，想起几个节点起几个"的诉求。**单进程 ≠ 不能水平扩展**——
扩展的单位是实例，真正的阻碍不是"没拆服务"，而是**实例重量与状态残留**：

| 阻碍 | 代码事实 | 后果 |
|------|---------|------|
| 实例启动重量 ∝ agent 总数 | `AgentManager.boot()` 遍历全部非 stopped agent 逐一 `_start_agent`（`manager.py` L471） | 每个节点的内存都被**全量** agent 占据，与该节点分到的流量无关；加节点只摊 CPU、不省内存 |
| 后台单例随实例重复 | cron / IM / 主动关怀 / 知识恢复在 `_boot_runtime()` 内启动 | 加节点会重复执行（正确性问题，非性能问题） |
| 本地盘状态 | 上传、知识库文档落 `~/.octop` | 新节点看不到旧数据 |
| SQLite | WAL 单机 | 共享数据层必须切 PG |

目标模型（示意图见会话内架构图）：**接入层无状态 N 副本 + agent 懒加载（每节点只装路由到自己的活跃 agent）+ 后台单例全局 1 份 + 状态全外置（PG + 对象存储）**。扩容 = 再加一个容器。

两条实现路线：

1. **同构轻副本（推荐）**：保持"API + agent 执行"同进程，仅做懒加载 + 后台门控 + 文件外置三项改造。agent 执行本质是等 LLM 流式（IO 密集），此模型下加节点即近似线性加容量；**不需要消息队列、不需要拆服务**。
2. **执行层剥离（真微服务）**：agent 执行独立 worker 池 + 任务队列，HTTP 层纯 API。仅当同构轻副本到达上限（500→1000+ 人均重度使用）再评估，涉及 harness 运行时改造，工作量最大。

关键认知：为"像微服务"而拆是伪需求；扩容能力取决于**每节点成本是否只随其负载增长**——懒加载 + 状态外置达成这一点后，N 个轻实例在行为上等价于微服务的水平扩展。
