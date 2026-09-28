# Octop 生产部署改造设计（MinIO + PostgreSQL/pgvector，2026-09-24）

> 目标形态：多实例 + 执行剥离的集群架构，落地在自建 **MinIO + PostgreSQL/pgvector** 上。
> 三项硬指标：**数据零丢失（RPO=0）、高可用（99.9%）、高并发（200 并发会话起步）**。
> 本文是三份前序文档（`scaling-500-users.md` / `microservice-scaling-1000.md` /
> `production-ha.md`）在本基础设施上的**落地设计**，改造项均标注来源。

---

## 0. 验收指标（不可妥协项）

| 指标 | 目标 | 验收方式 |
|------|------|---------|
| RPO | 0（任何单组件故障不丢已确认数据） | chaos 演练：杀主库/杀 MinIO 节点/杀任意实例后对账 |
| 可用性 | 99.9% | 30 天拨测 + 故障演练 RTO 达标（见 production-ha.md §3） |
| 并发 | 200 并发会话稳定 30 分钟 | Locust 走 SSE 聊天流压测，错误率 <0.1% |
| 流式延迟 | token 链路增量 ≤100ms（对比单实例基线） | 压测分位统计 |

## 1. 现有设施升级要求（前置，必须先做）

| 设施 | 现状 | 生产要求 | 不升级的后果 |
|------|------|---------|-------------|
| MinIO | 本地单实例（推测） | **纠删码分布式模式：≥4 节点 × ≥1 盘（或单机 ≥4 盘）**；开启 TLS；独立网卡/万兆 | 单实例 MinIO 是 SPOF——它承担全部工作区/附件/知识库文档，挂了=全站文件不可用，**直接违背高可用目标** |
| PostgreSQL | 本地单实例 + pgvector | **主备 + 自动切换**（Patroni + etcd，或双节点流复制 + 手工脚本）；`synchronous_commit=on`（同步备库）；pgBackRest WAL 归档（PITR） | 单实例 PG 挂 = 全站停 + 无 RPO 保障 |
| 负载均衡 | 无 | 2 × nginx（keepalived VIP）或云 LB | 入口单点 |

> 说明：MinIO 纠删码 `EC:4` 下可容忍 `(N/2)-1` 节点故障；单机多盘**只防盘故障、不防节点故障**。
> 零丢失目标要求 MinIO 至少 4 节点分布式部署。

## 2. 总体拓扑

```
            ┌──── LB ×2（keepalived VIP，TLS 终结，健康检查 /api/health）────┐
            │                                                               │
     ┌──────▼──────┐  ┌──────────┐  ┌──────────┐        ┌───────────────┐
     │ octop-api   │  │ octop-api│  │ octop-api │        │ octop-bg ×1   │
     │ （≥3，N+1） │  │          │  │          │        │ cron/IM/备份  │
     └──────┬──────┘  └────┬─────┘  └────┬─────┘        └───────┬───────┘
            │   回合任务（Procrastinate 队列，PG 承载）  │              │
     ┌──────▼──────────────▼───────────────────▼──────┐      │
     │                octop-agent ×3（N+1）             │◄─────┘（cron 触发的回合）
     │   harness 运行时 · agent 懒加载 · 并发上限 40/台  │
     └──────┬──────────────────────────────┬───────────┘
            │                              │
   ┌────────▼─────────┐          ┌─────────▼──────────┐
   │ PostgreSQL 16 HA  │          │  MinIO 集群 ×4      │
   │ 主+同步备（Patroni）│          │  纠删码 EC:4 · TLS  │
   │ pgvector · PITR    │          │                    │
   └────────┬─────────┘          └─────────┬──────────┘
            └──────── WAL 归档/PITR 备份 ────┘（独立 bucket）
```

- 实例规划（200 并发会话）：api ×3、agent ×3、background ×1，全部预留 +1 冗余能力；
- `OCTOP_HOME` 各实例独立本地盘（日志/回环文件/ssl），**禁止整目录 NFS**；
- 共享态只在三处：PG、MinIO、（可选）Redis。

## 3. 数据布局设计

### 3.1 PostgreSQL（一个实例簇承载五类数据）

| 数据 | 位置 | 说明 |
|------|------|------|
| 控制面（用户/agent/渠道/会话/轨迹事件/后台任务状态） | 默认 schema，迁移表管理 | 现有迁移机制 |
| agent memory（LangGraph checkpoint） | `agent_<id>` schema | harness-memory 已支持，切 PG 即启用 |
| **知识库向量**（新） | `knowledge_chunks` 表（见 §4.2 DDL） | 替换 SQLite sidecar |
| 回合任务队列 | Procrastinate 表（`procrastinate_jobs/workers`） | 与业务同库同事务 |
| PG 连接预算 | 每实例 pool `max_size=8`（现值）× 7 实例 + Procrastinate worker ≈ **60 连接** < 默认 `max_connections=100` | 压测后按需调 pool；不建议现在上 pgbouncer（连接数富余） |

HA 参数：`synchronous_standby_names` 指向同步备库（RPO=0 的来源）；`statement_timeout`/`idle_in_transaction_session_timeout` 防悬挂事务；主备切换由 Patroni 仲裁（etcd ×3）。

### 3.2 MinIO bucket 设计

```
octop-ws/                        # 唯一工作桶（BackendWorkspace 挂载）
  agents/<agent_id>/...          # 与本地 PathLayout 相对路径一一对应
    inbound/                     # 聊天附件（api/common/attachments.py → write_inbound）
    .env / workspace 文件
  knowledge/<kb_id>/docs/...     # 知识库文档原件
octop-backup/                    # pgBackRest WAL 归档 + 周期逻辑备份
                                 # ⚠️ 建议独立 MinIO 集群或至少独立磁盘组
```

- 接入方式：**控制台配置**（backend 表行：`endpoint`/`bucket`/`region`/`access_key`/`secret_key`，
  `region` 必填——`adapter.py` L50 校验，随便填如 `us-east-1`），S3 协议对 MinIO 原生兼容，
  **代码零改动**；
- MinIO 侧：bucket 开启 versioning（防误删/覆盖，配合回收策略）；TLS 强制；
- 访问路径：所有实例经同一 endpoint（MinIO 前置 nginx 均衡到 4 节点，或域名解析轮询）。

## 4. 应用改造项清单

### 4.1 纯配置（无代码）

| 项 | 内容 |
|----|------|
| 指向 PG | `OCTOP_DATABASE_URL`（或 config.json database 段）；集群模式硬前提 |
| 指向 MinIO | 控制台 → workspace backend 配置（见 §3.2）；上线前用 backend probe 探活验证 |
| 关闭内置 ACME | TLS 归 LB；`enable_api_docs=false` |

### 4.2 代码改造（按依赖顺序）

**① 知识库向量迁 pgvector**（来源：production-ha.md §4-4；多实例硬前置）

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE knowledge_chunks (
    chunk_id  TEXT PRIMARY KEY,          -- "{doc_id}:{ordinal}"，与现 SQLite 同规则
    kb_id     TEXT NOT NULL,
    doc_id    TEXT NOT NULL,
    ordinal   INTEGER NOT NULL,
    text      TEXT NOT NULL,
    embedding vector(1024) NOT NULL,     -- 维度 = 当前 embedding 模型；换模型需重建（见下）
    meta      JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX ON knowledge_chunks(kb_id, doc_id);
CREATE INDEX ON knowledge_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
-- 查询：WHERE kb_id=$1 ORDER BY embedding <=> $2 LIMIT k；会话内 SET hnsw.ef_search=64
```

- 新增 `PgVectorKnowledgeIndex`（实现与现 `KnowledgeIndex` 同接口：`replace_doc_chunks` /
  检索 `Hit`），按配置切换；**旧 SQLite sidecar 迁移脚本**：读 BLOB → 批量 INSERT
  （`COPY`，每批 1000）→ 建索引 → 对账 chunk 数；
- **维度策略**：部署时按 embedding 模型维度定型；换模型 = 新表 + 全量重嵌（列为运维流程，
  不做动态维度——pgvector 索引要求固定维度，诚实处理）；
- 文档原件写 MinIO（§3.2 路径），删除 `files.py` 的本地 `write_bytes` 路径。

**② 后台单例门控 + 选主**（来源：scaling-500-users.md P1 + production-ha.md §2.3）
`_boot_runtime()` 门控 + PG 会话级 advisory lock 选主 + 持锁心跳（TCP keepalive + `SELECT 1`）
+ 接管后 cron reconcile 补跑。cron 现值 `misfire_grace_time=60/300` 保持。

**③ AgentManager 懒加载**（来源：500 文档 §4）——boot 不再全量 `_start_agent`，
首次使用按需启动；前提 PoC 1（harness 跨进程 checkpoint 正确性）。

**④ 执行剥离**（来源：microservice-scaling-1000.md §4/§6）
`AgentExecutor` 抽象 + Procrastinate 队列 + `lock=octop-turn:{agent}:{thread}`（串行；
禁用 `queueing_lock`——那是去重，会丢消息）+
自愈已内置于 worker `_stalled_loop`（真死重派；假活由回合运行时上限 2100s 判死，D15）+
流回传走 PG NOTIFY + `list_from_seq`
（chunk 已全量落 trajectory 表，零丢失回放）。

**⑤ 可靠性小项**（来源：production-ha.md §4）：readiness 探针、连接池快速失败/退避、
worker `shutdown_graceful_timeout=900s`、METRICS 暴露 /metrics。

### 4.3 数据零丢失机制映射（每个写路径 → 保障）

| 用户数据 | 写路径 | 零丢失保障 |
|---------|--------|-----------|
| 聊天消息/轨迹/chunk | api/worker → PG | PG 事务 + 同步备库；SSE 按 seq 回放 |
| agent 执行状态 | worker → checkpoint → PG | 步骤级持久化；worker 崩溃由 stalled 检测重派续跑 |
| 附件/工作区文件 | api → MinIO | S3 写成功即持久（纠删码同步写多节点）；bucket versioning 防误删 |
| 知识库向量 | 索引任务 → PG | 与文档写 MinIO 分两步——先文档后索引，索引任务有 DB 状态可重放 |
| 回合任务 | api → Procrastinate | **与消息同事务入队**（envelope：事务回滚=任务不存在，事务提交=任务必达） |
| cron 触发 | background → repo 状态 | 接管 reconcile 补跑；投递幂等 |
| 配置/密钥 | 控制台 → PG（secret 表） | 同 PG 保障 |

## 5. 高并发设计

| 维度 | 设计 | 依据/预算 |
|------|------|----------|
| 并发会话 | 200 = agent ×3 × 上限 40 + 冗余 | harness 执行 IO 密集；单 worker 40 为压测初值，按 CPU 段调 |
| SSE 连接 | api ×3 各扛 ~70 长连接 + 断线重连风暴预留 | asyncio 单进程万级连接无压力，瓶颈在 CPU 段（序列化）——这是 api/agent 分离的原因 |
| PG 连接 | 7 实例 ×8 + Procrastinate ≈ 60 < 100 | §3.1；压测后调 |
| 向量检索 | HNSW（m=16, ef_construction=64, ef_search=64） | 200 并发下检索 P99 < 10ms 量级（万级 chunk）；chunk 百万级再分区 |
| MinIO | 4 节点聚合带宽；上传经 api 转发（现状） | 大文件高频场景后续可改预签名直传（列为二期，不在本期） |
| 事件回传 | PG NOTIFY 50ms 批量 flush | microservice 文档 §4.2 通道 A；不达标切 Redis |

## 6. 高可用与零丢失验证（上线门槛，全部必过）

1. 杀主 PG → Patroni 切备 → api 5xx 窗口 ≤120s → 对账：会话/chunk/任务零丢失；
2. 杀一台 MinIO 节点 → 读写正常（EC 降级）；同时杀两台 → 读降级写拒绝（预期行为）；
3. 杀 agent worker（SIGKILL，执行中）→ ≤90s 重派 → 回合从 checkpoint 续跑 → 客户端收到的帧序完整（seq 连续）；
4. 杀 background → ≤30s 接管 → cron reconcile 补跑 → 定时任务触发一次且仅一次；
5. api 滚动重启 → 在途 SSE 全部重连成功（Last-Event-ID）→ 用户零感知；
6. 全链路断电演练 → PITR 恢复到最近 WAL → RPO=0 验证。

## 7. 上线步骤与回滚

1. **基础设施**：PG HA 集群 + MinIO ×4 + LB 就绪，`CREATE EXTENSION vector`；
2. **新库初始化**：`octop init` 直连 PG（不迁 SQLite 旧数据——`rebind.py` 只切连接不搬数据，
   存量数据如需保留走 system_archive 导出→导入，或明确放弃）；配置 MinIO backend 并 probe 验证；
3. **代码上线**：按 §4.2 顺序（①pgvector → ②门控 → ③懒加载 → ④执行剥离 → ⑤可靠性项），
   每步 `make all` 绿 + PoC/演练；
4. **灰度**：双 api + 双 agent 拓扑跑 1 周（接入真实流量 10%→50%→100%）；
5. **回滚**：任何阶段可回退单实例形态（配置切回 SQLite/本地盘数据仍在原 OCTOP_HOME）；
   阶段 4 后回滚 = LB 摘除新实例，PG 数据不动。

## 8. 风险登记

| 风险 | 等级 | 对策 |
|------|------|------|
| PoC 1：harness 跨进程 checkpoint 正确性未验证 | 高（阻塞 ③④） | 最先执行；备选 agent 粘性路由 |
| MinIO 当前单实例 | 高 | §1 升级为 4 节点纠删码是本方案前置，不可跳过 |
| pgvector 维度与 embedding 模型绑定 | 中 | 换模型 = 重嵌全量（运维流程化）；索引重建期间检索退化为旧索引只读 |
| PG 同步提交的性能代价（写延迟 +1 RTT） | 中 | 同机房部署；压测确认 chunk/轨迹写入吞吐 |
| pgBackRest 备份与业务同 MinIO 集群 | 中 | 备份桶独立磁盘组/集群；否则一次存储级故障可能同时摧毁数据与备份 |
| 浏览器会话节点有状态 | 中 | 粘性路由（agent 维度）；二期独立浏览器服务 |
