# 阶段 2 部署方案（RemoteExecutor + Procrastinate）

> 更新时间：2026-09-25。适用范围：阶段 2 改造合入后的版本。
> 详细设计见 `docs/microservice-scaling-1000.md`；架构对照图见 `docs/architecture-original-vs-stage2.md`。

## 1. 核心变化

**交付物不变**——仍是同一个 Python wheel。变化只在启动方式：从"1 个进程包揽一切"变为"1 类 api 进程 + N 个 worker 进程"，用环境变量决定每个进程的角色。

| | 原版 | 阶段 2（remote 模式） |
|---|---|---|
| 启动命令 | `octop run` ×1 | `octop run`（api）+ `octop agent-worker` ×N |
| 数据库 | SQLite 或 PG | **必须 PG**（队列 + 事件回传 + 业务库） |
| api 节点内存 | ∝ agent 总数（全量预启动） | O(1)（不预启动） |
| 扩容 | 整体复制（后台任务会重复跑） | api / worker 各自加副本 |
| 兼容性 | — | 不设 `OCTOP_EXEC_MODE` 时行为与原版完全一致 |

## 2. 角色与环境变量

### api 节点（接客 + 派单）

```bash
export OCTOP_EXEC_MODE=remote
export OCTOP_QUEUE_DSN=postgresql://user:pass@pg-host:5432/octop
octop run
```

- 只收发 HTTP/SSE、把任务入队、消费回传事件；不预启动 agent。
- 可直接放 LB 后横向复制（SSE 需关闭代理缓冲，支持 `Last-Event-ID`）。
- ~~cron、IM 网关等后台单例仍在 api 节点——多副本时只能让其中一个跑后台~~
  **（阶段 3 已解决：api 节点 advisory lock 选主，后台单例全集群恰好一个 leader 执行；worker 以 `leader=False` 启动永不竞争。见 `docs/deployment-stage3.md`。）**

### agent 节点（干活，可 ×N）

```bash
export OCTOP_QUEUE_DSN=postgresql://user:pass@pg-host:5432/octop
octop agent-worker            # 可选：--concurrency 8（单 worker 并行回合数）、--debug
```

- 按需启动 agent、执行对话、chunk 每 50ms 落库 + NOTIFY 回传。
- CLI 只有 `--concurrency` / `--debug` 两个参数；**工作区位置不是 CLI 参数**——由 `OCTOP_HOME`（默认 `~/.octop`）与控制台的 workspace backend 配置（MinIO / 共享卷）决定，所有 worker 必须解析到同一份存储。
- 扩容即加进程；同一线程的对话由队列锁（`octop-turn:{agent}:{thread}`）保证串行，跨 worker 有效。
- **workspace 必须多 worker 共享**（NFS，或按既定方案把 workspace backend 指向 MinIO），否则文件读写会落到不同节点。
- **内存取舍（有意决策）**：worker 启动即全量预启动所有 agent harness（`prestart_agents=True`），因此 worker 内存 ∝ **agent 总数 × worker 数**，而非"活跃会话数"。这是有意换取两点：① DDL 安全——预启动在任意回合开始前完成 LangGraph checkpointer 建表/建索引，避免与队列连接死锁；② 首回合零冷启动（review P2-7：预热在 boot，不在用户的第一回合）。agent 数量多时靠加 worker 分摊内存，每个 worker 都是完整副本。

## 3. 最小生产拓扑（docker-compose 概念）

```yaml
services:
  lb:       # nginx / traefik：TLS 终结、SSE 关缓冲、按域名路由
    # ports: 443
  api:      ×1   # octop run（remote 模式；后台单例所在，暂勿多副本）
  worker:   ×2   # octop agent-worker（workspace 共享见 §2；共享卷或 MinIO backend）
  postgres: ×1   # 生产建议主备 + 同步复制 + pgBackRest（见 docs/production-ha.md）
  minio:    ×4   # workspace 共享存储（按 docs/deployment-minio-pgvector.md 接入）
```

扩容口诀：**聊天并发涨 → 加 worker；Web 请求涨 → 加 api（阶段 3 后台选主已落地，api 可安全多副本）。**
可直接使用的集群编排见 `docker/docker-compose.cluster.yml` + `docs/deployment-stage3.md`。

## 4. PG 压力评估（不引入 Redis 的依据）

- 回传为 **50ms 批量写**（实测 p95 延迟 36~51ms），每路活跃流约 20 INSERT/s + 20 NOTIFY/s。
- 峰值 200 路并发流 ≈ 4000 单行 INSERT/s（约 1~2 MB/s）+ 4000 NOTIFY/s，远低于单机 PG 能力上限。
- NOTIFY 为 PG 内存异步队列，单条微秒级开销。

**三个收敛点（阶段 3 已全部落地，见 `docs/deployment-stage3.md`）**：
1. ~~`turn_events` 表只增不删~~ → 已加 `created_at` 保留期清理（默认 1800s）。
2. ~~每路活跃流占 2 个专用连接~~ → api 侧已收敛为共享 LISTEN hub（1 连接）+ 共享拉取池（默认 8），连接数 O(节点数)。
3. ~~NOTIFY 为全节点广播~~ → 共享 hub 已复用单一 LISTEN 连接。

出现单表写入 >5 万次/秒或亚毫秒延迟需求时，再评估引入 Redis；当前阶段 PG 是更简单可靠的选择。

## 5. 部署注意事项

1. **首次启动顺序**：任意角色都会幂等建表（advisory lock 保护）；但建议先起 1 个节点完成 schema 初始化后再横向铺开。procrastinate 自带 schema 非幂等（裸 `CREATE TYPE`），代码已处理，勿绕过。
2. **worker 启动预热（`prestart_agents`）**：worker 启动即全量加载所有 agent harness（含 LangGraph checkpointer 建表/建索引——这正是避免 DDL 与队列连接死锁的方式，取代早期设想的独立 warmup 钩子），启动到就绪有几秒延迟，健康检查应留余量。
3. **代理变量**：worker 进程访问 LLM 上游时，若环境带 `HTTP_PROXY` 而上游在内网，需剥离代理变量（联调实测坑）。
4. **监控项**：`procrastinate_jobs` 积压深度、`turn_events` 表大小与行数、`pg_stat_activity` 连接数、worker 存活（`octop agent-worker` 进程 + 队列心跳）。
5. **回滚**：去掉 `OCTOP_EXEC_MODE` 重启 api 即回到原版单进程行为；队列与事件表残留数据不影响单机运行。
