# 阶段 3 部署方案（选主 + 压测定容 + 集群编排）

> 更新时间：2026-09-25。前置：阶段 2（见 `docs/deployment-stage2.md`）。
> 机制设计见 `docs/microservice-scaling-1000.md`；可靠性审计见 `docs/production-ha.md`。

## 1. 本阶段解决什么

阶段 2 遗留的三件事，本阶段全部落地：

| 项 | 阶段 2 状态 | 阶段 3 状态 |
|---|---|---|
| 后台单例（cron / IM / proactive / 知识恢复） | api 多副本会重复跑 | **PG advisory lock 选主**，全集群恰好一个 leader 执行 |
| `turn_events` 只增不删 | 无清理 | `created_at` 保留期清理（默认 1800s），worker 周期执行 |
| api 侧连接 O(并发流) | 每流 2 个专用连接 | **共享 LISTEN hub（1 连接）+ 共享拉取池（默认 8）**，O(节点数) |

## 2. 选主机制（`src/octop/infra/cluster/leader.py`）

- **会话级 advisory lock**（`pg_try_advisory_lock`，key 918273647）持在一条专用连接上：会话死 = 锁自动释放，无 TTL、无时钟偏移、无脑裂窗口。
- `LeaderRunner` 抢到锁 → 启动挂接的后台服务；会话断（心跳 `SELECT 1` 失败）→ 停服务 → 重新争抢。备节点按 `poll_interval`（默认 10s）轮询，接管延迟为秒级。
- 挂接的单例：cron 调度器、IM 频道轮询（Gateway `im_active` 门控）、proactive-care、知识索引恢复。
- **角色边界**：api 节点（`octop run`）参与选主；agent-worker（`octop agent-worker`）以 `leader=False` 启动，永不竞争——后台单例只落在 api 节点。
- 多副本 api 天然支持滚动升级：旧 leader 死，新 leader 秒级接管（cron misfire 窗口 60s 内可兜住）。

## 3. 集群编排（docker-compose）

```bash
docker build -f docker/Dockerfile -t octop:latest .
docker compose -f docker/docker-compose.cluster.yml up -d --scale api=2 --scale worker=2
```

拓扑与要点：

| 组件 | 说明 |
|---|---|
| `lb`（nginx） | SSE 关缓冲（`proxy_buffering off`）+ 长超时 + docker DNS 运行时解析 upstream（`docker/cluster-lb.conf`） |
| `api` ×N | `OCTOP_EXEC_MODE=remote`；参与选主；无固定端口/容器名，便于 `--scale` |
| `worker` ×N | `octop agent-worker`（`leader=False`）；不参与选主 |
| `postgres` | 队列 + 业务库 + pgvector；生产换主备 + PITR（`docs/production-ha.md`） |
| `minio` | workspace/附件/知识库文档；**生产须 ≥4 节点纠删码**（`docs/deployment-minio-pgvector.md`），compose 内单实例仅适合开发验证 |

workspace 共享：控制台把 workspace backend 指向 MinIO（backend 表配置 + probe 验证）后，worker 即无状态可任意调度；未配置前 compose 用共享卷 `octop_workspace`，多 worker 仅限单宿主机。

## 4. 压测与容量（`scripts/stage3/run_stage3.py`）

真 PG + 真 procrastinate + 真 HarnessAgent + 伪 LLM（4ms token 间隔），40 路并发流走全链路（RemoteExecutor → 队列 → 2 worker → 回传），`scripts/stage3/result.json` 为最新证据：

| 指标 | 数值 |
|---|---|
| 吞吐 | 40 流 / 5.3s ≈ **450 流/分钟**（2 worker × 并发 8） |
| 首 chunk 延迟 | p50 ≈ 1.26s，p95 ≈ 3.25s |
| 总延迟 | p50 ≈ 3.1s，p95 ≈ 5.1s |
| PG 连接占用（峰值，按进程归因） | api 13 / 每 worker 17 / hub 1，**总计 47，全部有界** |
| 事件清理 | 过期行删除、新鲜行保留 ✔ |
| 选主 | 单 leader + leader 停止后备节点自动接管 ✔ |

关键回归：压测曾暴露 api 侧冷启动竞态（并发首用各建一套队列/池，连接打满 `max_connections`），已修复并加双单测锁定（`test_concurrent_cold_start_creates_single_*`）。

**容量结论与边界**：

- 单机 PG 下，瓶颈不在 PG 写入（阶段 2 实测 200 路约 4000 单行 INSERT/s），而在 worker 执行并发与 LLM 上游；扩容口诀不变：并发涨 → 加 worker。
- api 侧连接现为常数（hub 1 + 拉取池 8 + 队列池 ≈ 13），**连接数不再是扩容变量**；200 路并发流在单机 PG 的 `max_connections=100` 预算内（api×2 + worker×N 均有界）。
- 诚实边界：上表为 40 路 × 秒级压测（机制级验证）。200 并发会话 × 30 分钟的容量承诺需在生产硬件 + 真 LLM 上按 `docs/microservice-scaling-1000.md` §7 退出标准复测。

## 5. 监控与运维增量

1. 选主事件：`octop-leader-runner` 日志（leadership acquired / lost）——接管频繁说明 PG 或网络不稳。
2. `pg_locks` 中 advisory lock key 918273647 的持有者即当前 leader。
3. `turn_events` 表行数应稳定在活跃流 × 每流 chunk 数量级，持续增长说明清理任务未跑（worker 侧）。
4. 其余监控项沿用 `docs/deployment-stage2.md` §5。
