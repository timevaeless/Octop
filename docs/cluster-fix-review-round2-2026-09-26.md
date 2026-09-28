# Octop Cluster 改造 · 第二轮修复评审（2026-09-26）

> 评审对象：`55d09315 → 3a90749a`（8 个新提交）
> 方法：读 diff + 真 PG 端到端探针 + 单测（74 passed）
> 一句话结论：**这一轮质量很高——我上轮提的 3 个 🔴 里，2 个（D15/D9）真修好并实测通过，D3 也按方案 A 落地了；文档对齐基本完成但漏了 1 处同类的。** 另外要更正我上轮的一个错误建议。

---

## 0. 先更正我上轮的一个错误

上轮我说 S3 的正解是 procrastinate 自带的 `job_timeout`（"组件自带"）。**这个说法是错的**，他们核实后指出：**procrastinate 3.10.0 全包 grep `job_timeout` 零命中**。我本机复核确认：

```
procrastinate.__version__ = 3.10.0
grep -rn job_timeout .venv/.../procrastinate/  → 零命中
```

所以自建运行时上限是**唯一可行**的路，他们的选择是对的。

---

## 1. 本轮 8 个提交

| 提交 | 内容 | 评价 |
|---|---|---|
| `af0cd4f2` | 归档我上轮的整体评审 | — |
| `49cff993` | **D15 回合运行时上限**（S3 兜底） | ✅ **实测通过**，见 §2 |
| `5baac968` | 文档对齐（自愈/队列锁表述） | 🟡 基本完成，漏 1 处，见 §5 |
| `266254bc` | 演进图集第 10 幕 + 缺陷清单补录 D15 | — |
| `c4be23b5` | **D3 在途回合续接**（thread→job 落库 + attach） | ✅ 方案 A 落地，见 §3 |
| `f7949891` | 演进图集第 9 幕 + production-ha §2.1 修订 | — |
| `17476b64` | **D9 LB 健康检查**（readiness + upstream 被动摘除） | ✅ 见 §4 |
| `edc7fcba` / `574bf830` / `3a90749a` | 演进图集第 11/12 幕 + **D3 前端 WS 续流** + 文档 | ✅ |

---

## 2. 🔴→✅ D15：S3 假活卡死**真的堵上了**（端到端实测）

### 实现（`remote.py:1036-1053`）
把回合执行抽成 `_run_turn()`，外层用 `asyncio.wait_for(_run_turn(), timeout=TURN_JOB_MAX_RUNTIME_SECONDS)`；超时 → `RemoteTurnError` → 走既有 D13 失败路径发错误哨兵 → 作业判失败 → **线程锁释放**。`None` 可关闭，参数从 `AgentWorker`/`build_worker_app` 透传。

### 端到端探针（真 PG + 真 procrastinate + 真 worker）
`.workbuddy-ai/tmp/probe/r2_probe_d15_lock_release.py`（我本轮新写）：

```
[1] 投递挂起作业 A id=1  (lock=octop-turn:agent-1:thread-D15, 运行时上限=0.4s)
[2] 跑 worker（wait=False，队列空即退出）…
[3] 挂起作业 A 执行后 -> (1, 'failed', 'octop-turn:agent-1:thread-D15')
    ✅ 已判失败（锁应已释放）
[4] 事件哨兵 kinds=['__turn_error__']
    ✅ 错误哨兵已发
[5] 映射表残留=[]  (✅ 已清理)
[6] 同线程后续消息 B 认领 -> (id=2, status=doing)  ✅ 锁已释放，可执行
最终作业表: [(1, 'failed'), (2, 'succeeded')]
```

**这条链正是单元测试没覆盖的那段**（`test_turn_runtime_bound.py` 自己的 docstring 就写了 "the real lock-release round trip lives in the stage-3 probe scripts"）——我用真组件把它补上了：**挂起 → 判死 → 哨兵 → 映射清理 → 同线程后续消息可认领**，全通。

### 附带修好的
`_stalled_loop` 的 docstring 现在**明确写了 S3 归 D15 管**（`remote.py:1283-1298`），不再声称心跳覆盖卡死；`TURN_JOB_MAX_RUNTIME_SECONDS` 注释也改对了。上轮我提的两处"注释撒谎"已消除。

### 仍要注意的边界
`asyncio.wait_for` 超时后会**取消内层任务并等待其完成**。若内层卡在**不可取消的阻塞 C 调用**（而非 `await`），`wait_for` 会一直等下去 —— 运行时上限对"纯 Python/网络 hang"有效，对"同步阻塞"不硬。生产里 LLM/工具/DB 都是 async I/O，属于前者，可用；但值得在文档里点一句。

---

## 3. D3：在途回合续接（方案 A）— 设计对，落地干净

**机制**：新表 `octop_turn_active_jobs`（`thread_id` 主键）；worker 认领即 UPSERT、`finally` 按 `job_id` 守卫删除（不误删排队新任务的认领）；`RemoteExecutor.active_job_id()` + `attach(job_id)` 从 seq 0 回放已持久化事件 + 共享 `NotifyHub` 跨节点唤醒续流。

**三个入口都接上了**：
1. 新 SSE 端点 `GET /agents/{id}/threads/{id}/turn/stream`（local 模式 404）
2. WS `subscribe` 时查映射 → 命中则 relay 进 hub + 先报 `turn_status.active=true`（前端零改动）
3. `_remote_relay_tasks` 单线程单 relay 守卫，防并发 subscribe 双份投递

**评价**：`claim` 在 try 之外、`clear` 在 `finally` 里且 job 守卫 —— 顺序正确。best-effort 设计（映射失败不影响回合，事件表仍是事实源）也对。测试 8 项含计划点名的 `test_api_crash_continues_turn`。

### ⚠️ 一个真实缺口：映射表**没有孤儿清理 / TTL**
`grep ACTIVE_JOBS_TABLE` 全 `src/` 只有 3 处用途：`INSERT`(claim) / `DELETE`(clear) / `SELECT`。**没有任何清理**。

- worker 被 **SIGKILL** 时 `finally` 不执行 → 映射行残留；
- 正常路径下它会自愈：`_stalled_loop` 把作业重派 → 新 worker `claim()` 用同 `thread_id` UPSERT → 完成后 `clear()` 删掉；
- **但**若该作业再也不会执行（重试耗尽/永久失败），映射行就**一直挂着**。此时客户端 `attach` 会拿到一个永远不会产生新事件的 `job_id` → 读者干等满 **1800s 看门狗**才报错，而不是"快速失败"。

**建议**（低成本）：`attach` 前用 procrastinate 查一下该 `job_id` 的状态，非 `doing` 直接返回"无在途回合"；或给映射表加 `claimed_at` TTL 清理（列已有 `claimed_at`，可直接用）。

---

## 4. D9：LB 健康检查 — 干净

- `/api/health/ready`（无鉴权）：DB 连通 **且** 迁移版本匹配才 200，否则 503 —— 区分"活着"与"能服务"。
- `migrate.py` 新增公开 `current_schema_version`（严格读，异常上抛）与 `latest_schema_version`；私有 `_current_version` 吞异常语义不变 —— **没有破坏既有语义**，好。
- `cluster-lb.conf` 改成 `upstream` 块 + `resolve` 参数：既保留 `--scale` 免重载，又拿到 `max_fails=2/fail_timeout=10s` 被动摘除 + `proxy_next_upstream`（连接失败/5xx 换副本）。注释明确写了"非幂等请求只在未发出时重试，聊天消息不会重复" —— **这正是我上轮担心过的点，他们主动写清楚了**。

小注：`resolve` 需要 nginx ≥ 1.27.3；compose 用 `nginx:1.27-alpine`（浮动到最新 1.27.x，满足）。若有人钉死更早的 1.27.x 会失效，建议注释里点明版本下限。nginx 配置本机无 docker 未跑 `nginx -t`（已注明）。

---

## 5. 文档对齐：修了 4 处，**漏了 1 处同类的**

### 已修（`5baac968` + 后续）
| 位置 | 现在 |
|---|---|
| `production-ha.md:67-73` | ✅ 假活不重派、由 D15 运行时上限兜底（不再说"心跳覆盖假活"） |
| `production-ha.md:154-156/162` | ✅ 第 2 项标记"已落地：内置 `_stalled_loop`" |
| `production-ha.md:168` | ✅ 第 8 项纠偏：用 `lock` 串行、禁 `queueing_lock`（D10） |
| `microservice-scaling-1000.md:174` | ✅ 同步 D15 |
| `deployment-minio-pgvector.md:136-137` | ✅ 去掉"必配周期任务"，同步 D15 |

### ❌ 漏网（本轮新发现）
**`docs/production-ha.md:130`** 仍写着：

```
| agent 执行 | 不可能 | SKIP LOCKED 认领，一个任务只被一个 worker 持有；queueing_lock 保证同 thread 串行 |
```

这句和刚在 `:168` 纠偏的内容**完全相反**（`queueing_lock` 是去重、会丢消息；`lock` 才是串行）。**同一个文件里，一处已改对、一处仍是错的。**

**根因**：这次是"逐处修"而不是"按关键词全量扫"。上轮我在 skill 里写过的教训正是这个——**改文档前先 `grep` 关键词把所有同类位置捞干净**。他们 `grep queueing_lock` 一遍就会发现 `:130`。

> 我全量 grep 复核：`docs/` 里 `queueing_lock` 的错误用法**只剩 `production-ha.md:130` 这一处**（`microservice-scaling-1000.md` 本来就用的 `lock`，是对的）。

---

## 6. 上轮 P3/P4 的现状（未动）

| 项 | 现状 |
|---|---|
| **D11 分层违规** `infra/` → `api/` | ❌ 仍在（`remote.py:105`，行号从 103 移到 105） |
| **worker 节点构造 `RemoteExecutor`** | ❌ 仍在（`server.py:392`，`execution_mode=="remote"` 无条件创建，无 `_leader_enabled` 守卫） |
| **D5 `pg_advisory_lock` 阻塞无超时** | ❌ 仍在（`migrate.py:1981`，仍是阻塞版，无 `pg_try_advisory_lock`/超时/日志） |
| **D2 watcher 重连失败不清 `_conn`** | ❌ 仍在（未动） |
| **`reload_channels_from_db` 兼作 start/stop handle** | ❌ 仍在（未动） |

这些都是上轮的 P3/P4，本轮精力都在 D15/D3/D9，合理。列出来提醒别丢。

---

## 7. 小项

- **D15 超时消息格式化**：`f"turn exceeded {job_runtime_seconds:.0f}s"` 对 `0.4` 打印成 `"0s"`（探针日志可见）。生产是 2100 → "2100s" 正常，纯 cosmetic。
- **D3 已知边界（他们自己标注了）**：续接是**裸 chunk 流**；`thread_messages` 投影与轨迹落库仍由原读端承担，**投影级续接待后续**。`production-ha §2.1` 已同步说明。这是诚实的边界声明，认可。

---

## 8. 进度总表（截至 `3a90749a`）

| ID | 内容 | 状态 |
|---|---|---|
| D1 | 自愈误重派收窄 | ✅ 已修（+D15 补上假活） |
| D2 | 渠道跨节点传播 | ✅ 已修 |
| D3 | 在途回合续接 | ✅ **本轮已修**（方案 A；映射无 TTL 待补） |
| D4 | 阶段 0 多副本门控 | ⬜ 已按岔路一降级 |
| D5 | 迁移跨进程锁 | ✅ 已修（阻塞无超时待补） |
| D6/D7/D8 | worker 文档/prestart 相关 | ⬜ 未动 |
| D9 | LB 健康检查 | ✅ **本轮已修** |
| D10 | 队列锁文档纠偏 | 🟡 基本完成，漏 `production-ha.md:130` |
| D11 | 分层违规 infra→api | ⬜ 未动 |
| D12 | 脚本硬编码路径 | ⬜ 未动 |
| D13 | open 失败发哨兵 | ✅ 已修 |
| D14 | 重派逐作业容错 | ✅ 已修 |
| D15 | 回合运行时上限（S3） | ✅ **本轮已修，实测通过** |

**8 项已修 / 1 项基本完成 / 4 项未动（D6/D7/D8/D11/D12 中的低优先级）**。

---

## 9. 建议

1. **补 `production-ha.md:130`**（一行），并**养成"改文档先 grep 关键词全量扫"的习惯**——这次就是漏扫。
2. **D3 映射加孤儿处理**：`attach` 前查 job 状态，非 `doing` 快速失败；或用已有的 `claimed_at` 做 TTL。
3. **D11 分层**（`remote.py:105` 下沉 `json_chunk_default` 到 `infra/utils/`）+ **worker 不建 `RemoteExecutor`**（`server.py:392` 加 `_leader_enabled` 守卫）—— 都是几行的事。
4. **D5 迁移锁**改 `pg_try_advisory_lock` + 有限重试 + 等待日志，避免持锁节点挂住时其余节点静默无限阻塞。
5. 补两条真环境探针：**双进程真 PG 验证 D2 跨节点传播**、**双进程同时 `run_migrations` 验证 D5 串行**（目前两者都只有单测/mock）。

---

## 10. 诚实边界

- D15 已用真 PG + 真 procrastinate 端到端实测（§2），**结论可靠**。
- D3 的"跨节点"结论来自**读代码 + mock 测试**（`reader_factory` 注入），**未做双进程真 PG 端到端**；`test_api_crash_continues_turn` 用 fake registry + ReplayReader 模拟，证明的是"映射能定位到同一 job 并重放"，不是"真跨进程"。建议补真环境验证。
- D9 的 nginx 配置**未跑 `nginx -t`**（本机无 docker），按官方语法人工核对。
- 单测：本机跑相关子集 **74 passed**（`tests/unit/cluster` + `test_remote_executor` + `test_executor` + `test_migrate_lock`）；全量套件在本沙箱被 SIGTERM（太大），以对方自报 3928 passed 为准。

---

## 11. 复现命令

```bash
cd /Users/timevaeless/Documents/Work/Octop-cluster

# D15 端到端（挂起→判死→哨兵→映射清理→同线程可认领）
docker exec octop-postgres psql -U octop_admin -d octop_probe_rv \
  -c "TRUNCATE procrastinate_jobs, procrastinate_events, procrastinate_workers RESTART IDENTITY CASCADE;" \
  -c "DELETE FROM octop_turn_events;" -c "DELETE FROM octop_turn_active_jobs;"
TMPDIR=.workbuddy-ai/tmp .venv/bin/python .workbuddy-ai/tmp/probe/r2_probe_d15_lock_release.py

# 相关单测（74 passed）
mkdir -p .workbuddy-ai/tmp/pytest
TMPDIR=.workbuddy-ai/tmp .venv/bin/python -m pytest \
  tests/unit/cluster tests/unit/agents/test_remote_executor.py tests/unit/agents/test_executor.py \
  tests/unit/db/test_migrate_lock.py -q -p no:cacheprovider --basetemp=.workbuddy-ai/tmp/pytest

# 文档漏网复核
#   Grep 工具搜 "queueing_lock" → 只剩 production-ha.md:130 是错的
```
