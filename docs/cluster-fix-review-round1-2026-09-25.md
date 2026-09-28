# Cluster 改造 · 第一轮修复评审（2026-09-25）

> 评审对象：`efc958e1` — `fix(cluster): 第一批缺陷修复——自愈收窄+open哨兵+重派容错`
> 评审方法：读 diff + 真 PG scratch 库 + 真 procrastinate 3.10 + 生产类 `AgentWorker`，用探针把每条"已修复"声明变成可证伪的实测判据。
> 评审结论：**2 项真修好了（D13/D14），1 项修了一半（D1）——把双跑堵死了，但顺手把"卡死自愈"整条删掉，且声称的替代者覆盖不了关键场景。**

---

## 0. 一句话结论

这轮把"长回合被误重派导致双跑"堵住了（这是对的），但代价是**把唯一能救"worker 还活着、但回合卡住"的兜底路径整条删掉**，而它声称的替代者（心跳检测）**根本扫不到这种作业**。

结果是：原 P0-1 那条"该线程之后所有消息永久不执行"的经典失败形态，**换了个入口重新出现**——这次不是"worker 崩溃"，而是"worker 好好的、就是回合卡住了"。api 侧用户 30 分钟后会看到报错，但**线程锁永不释放**。

---

## 1. 三种状态（先分清哪些真生效）

| 状态 | 项 | 说明 |
|---|---|---|
| ✅ **已经生效** | **D13** open 失败发哨兵 | 实测：读者 **0.02s** 拿到错误（对照组要等满 3s 看门狗）。真修好了。 |
| ✅ **已经生效** | **D14** 重派逐作业容错 | 实测：一个作业抛错，同轮第二个照常重派（`retried=1`，作业表 `[(5,'succeeded'),(6,'todo')]`）。真修好了。 |
| ⚠️ **修了一半** | **D1** 自愈收窄 | 双跑**确实**消除了（旧探针复跑翻转）。但"卡死自愈"被**整条删除**，替代者不成立 → 新洞。见 §2。 |
| 🔴 **有洞没堵** | **D1 的副作用** | "worker 假活（存活 + 心跳正常 + 回合卡住）"→ 线程**永久卡死**，无任何自愈。见 §3。 |
| 🔴 **有洞没堵** | **文档与代码相反** | 3 处文档仍写"运行时兜底覆盖 worker 假活 / 双检测"，代码已删。见 §4。 |

---

## 2. 逐条核实：D1 / D13 / D14

### D1 — 自愈误重派收窄（`retry_stalled_jobs_once`，`remote.py:1160-1218`）

**提交声明的原话：**

> 运行时兜底(`nb_seconds=2100`)改为仅监控不重派：worker 仍存活的长回合不再被误重派，消除双跑与同线程串行化失效。**worker 真死/卡死由 heartbeat 检测覆盖**；`worker_id IS NULL` 孤儿同样被 heartbeat 覆盖。

拆成两个可证伪的子断言：

| 子断言 | 实测结果 | 判定 |
|---|---|---|
| ① 存活 worker 的长回合不再被误重派（消除双跑） | 旧探针复跑：`[6] -> retried=0`、`[7] -> FIXED (D1)` | ✅ **成立** |
| ② "worker 真死/**卡死**由 heartbeat 检测覆盖" | 三场景探针：S3（存活+心跳正常+回合卡住）→ **心跳检测器零命中** | ❌ **证伪** |

**代码现状**（`remote.py:1199-1218`）：运行时检测器（`nb_seconds`）保留查询但**只记 `logger.debug`、绝不重派**；注释里明确写着"`worker_id IS NULL` 孤儿…由 heartbeat 查询覆盖，所以运行时路径不增加任何有效重派"。

问题就出在这句注释的**后半段假设**上——它假设"所有值得重派的作业都能被 heartbeat 检测器扫到"。这个假设**不成立**，见 §3。

### D13 — `publisher.open()` 纳入 try（`remote.py:895-936` + `TurnEventPublisher.fail:839-867`）

**实测（真 `_execute_turn` + 注入 open 失败）：**

```
_execute_turn 抛出: RuntimeError: simulated open failure (transient PG)  (elapsed 4.39s)
事件表行数 = 1
  seq=1 payload={"c": "{\"type\": \"__turn_error__\", \"error\": \"RuntimeError: ...\"}", ...}
读者结果: RemoteTurnError: ...  (elapsed 0.02s)   ← 远小于 1800s 即通过

对照组：无任何事件时读者只能等看门狗
读者结果: RemoteTurnError: no worker events for 3s ...  (elapsed 3.02s)
```

| 子断言 | 实测结果 | 判定 |
|---|---|---|
| open 失败仍能发错误哨兵 | 事件表 1 行，读者 0.02s 命中 | ✅ **成立** |
| `fail()` 在 `conn is None` 时独立建连接 | best-effort 分支实测命中 | ✅ **成立** |
| 读者不再空等 1800s 看门狗 | 0.02s vs 对照 3.02s | ✅ **成立** |

**结论：D13 真修好了。** 唯一小注：`fail()` 的独立连接失败只记日志（best-effort），这是合理取舍，不是缺陷。

### D14 — 重派循环逐作业容错（`remote.py:1187-1198`）

**实测（两作业，第一个并发 finish 后 `retry_job` RAISE）：**

```
两个心跳停跳的作业: [5, 6]
retry_stalled_jobs_once -> 1   （期望 1：只成功重派第二个）
作业表: [(5, 'succeeded'), (6, 'todo')]
```

| 子断言 | 实测结果 | 判定 |
|---|---|---|
| 单作业 RAISE 不中断本轮扫描 | `retried=1`，第二个照常重派 | ✅ **成立** |
| 失败仅记日志（`logger.exception` + `continue`） | 探针顶部出现被容忍的 `ConnectorException`（预期） | ✅ **成立** |

**结论：D14 真修好了。**

### 回归测试

```
tests/unit/cluster + test_remote_executor.py + test_executor.py
→ 49 passed, 1 warning in 4.00s
```

（提交自报全量 `3891 passed + 18 skipped`；本次只跑相关 3 个文件，全绿。）

---

## 3. D1 的证伪：三场景探针

**探针**：`.workbuddy-ai/tmp/probe/r1_probe_stall_selfheal.py`
**原理**：真 PG scratch 库 + 真 procrastinate + 生产类 `AgentWorker.retry_stalled_jobs_once`，构造三种"作业卡住"形态，看自愈能否覆盖。

### 三种形态

| 形态 | 场景 | 期望 |
|---|---|---|
| **S1 孤儿** | worker 进程被 SIGKILL，行已被 `prune_stalled_workers` 清掉 → `worker_id IS NULL` | 应恢复 |
| **S2 真死** | worker 行还在，但心跳停跳（>60s） | 应恢复 |
| **S3 假活** ★ | **worker 存活、心跳新鲜，但作业 `doing` 已超 2100s**（LLM 上游 hang / 工具不返回 / DB 挂住） | 关键：应恢复，否则线程永久卡死 |

### 实测输出（清空队列表后跑，干净结果）

```
constants: STALLED_HEARTBEAT_SECONDS=60.0 TURN_JOB_MAX_RUNTIME_SECONDS=2100.0

[S1] worker 行已 prune; 作业 -> (1, 'doing', None)
[S2] worker#2 心跳已回退 180s; 作业 -> (2, 'doing', 2)
[S3] worker#3 存活且心跳新鲜, 作业 doing 已超 2100s -> (3, 'doing', 3)

--- 一次自愈扫描 ---
retried=2
    S1 孤儿: (1, 'todo', None)     ← 恢复 ✅
    S2 真死: (2, 'todo', 2)        ← 恢复 ✅
    S3 假活: (3, 'doing', 3)       ← 不重派（D1 生效）

--- S3 能否自愈？连续 6 轮扫描，每轮刷新 worker#3 心跳（模拟存活）---
    cycle 1: retried=0  job3=(3, 'doing', 3)
    cycle 2: retried=0  job3=(3, 'doing', 3)
    cycle 3: retried=0  job3=(3, 'doing', 3)
    cycle 4: retried=0  job3=(3, 'doing', 3)
    cycle 5: retried=0  job3=(3, 'doing', 3)
    cycle 6: retried=0  job3=(3, 'doing', 3)
    heartbeat 检测器命中: []       ← ★ job3 不在列 = 心跳检测器根本扫不到

--- S3 同线程的后续消息（用户下一条消息）能否被认领？---
    第 1 次认领 -> None
    第 2 次认领 -> None
    第 3 次认领 -> None
    job3=(3, 'doing', 3)  job3b=(4, 'todo', None)
```

### 结论

1. **S1/S2 仍能自愈**（死掉的 worker 被正确兜底）——这部分是好的。
2. **S3 永久卡死**：连续 6 轮扫描 `retried=0`，作业永远停在 `doing`。
3. **替代者不成立**：`heartbeat 检测器命中: []` —— 心跳检测器**扫不到** S3。原因很直接：S3 的 worker 心跳是**新鲜的**（进程健康、事件循环正常），心跳检测器按"心跳停跳"判定，自然不命中。
4. **后续消息永久排队**：同线程下一条消息（job3b）三次认领全部 `None`，永远停在 `todo`。

### 为什么这是真实风险，不是纸面推演

- **触发场景真实**：LLM 上游 hang（TCP 连上但不再吐 token）、工具调用永不返回、DB 查询挂住 —— 这些都会让"事件循环健康、心跳照常"的 worker 卡在一个回合里。
- **api 侧救不了它**：读者看门狗（1800s）只做一件事——`raise RemoteTurnError` 给调用方（`remote.py:478-485`）。**它不发 cancel**。所以用户 30 分钟后看到报错，但 worker 的回合照跑，作业照 `doing`，锁照占。
- **worker 侧也没有回合超时**：全 `remote.py` 里唯一的时间约束就是那个 api 侧 1800s 看门狗（`turn_timeout`）；**没有任何 worker 侧的回合超时**能把它踢出来。
- **无自愈**：`retry_stalled_jobs_once` 只认心跳停跳，S3 心跳正常 → 永不重派。

**净效果**：这条线程从"卡住那一刻"起**永久不可用**——之后所有消息静默排队、永不执行。这正是原 P0-1 要堵的那条失败形态，只是入口从"worker 崩溃"换成了"worker 假活"。

**严重度判定：P1**（对比修复前的"良性误重派/双跑"——那是可自愈的 at-least-once 代价；这次是**功能永久不可用**，更重）。

---

## 4. 文档与代码相反（新增 3 处欠账）

修复删掉了"运行时兜底重派"，但 3 处文档仍按**旧行为**描述，读者会据此以为 S3 被覆盖：

| 位置 | 原文（仍称已覆盖） | 现状 |
|---|---|---|
| `docs/production-ha.md:53` | `+ 超 2100s 的 doing 作业兜底（覆盖"worker 假活"），retry_job() 重派释放锁` | ❌ 代码已删重派，只剩监控 |
| `docs/microservice-scaling-1000.md:173` | `周期 get_stalled_jobs + retry_job 自愈（心跳 60s + 运行时 2100s 双检测）` | ❌ 不再是"双检测"，运行时那半只监控 |
| `docs/deployment-minio-pgvector.md:136` | `必配 retry_stalled_jobs 周期任务（1min）` | ⚠️ 与 `production-ha.md:137/144` 仍称"未接入"自相矛盾 |

---

## 5. 修复方向建议

原评审（`cluster-review-2026-09-25.md:657-658`）给的方案是"**只做 `worker_id IS NULL` 的那一类重派**"；这轮修复**走反了**——不是收窄，而是把运行时路径整条改成只监控。可选方案（按推荐度）：

1. **【推荐】补 `job_timeout`（procrastinate 自带）** —— 这是原评审给 S3 的正解（`cluster-review:95` "补上…+ 设 `job_timeout`"）。给回合任务设一个**明显高于正常回合上限**的 `job_timeout`，让 procrastinate 自己把卡死作业判失败 → 释放锁 → 触发重试。既覆盖 S3，又不引入双跑（超时即判死，不存在"原 worker 还在跑"）。
2. **运行时兜底按 `worker_id IS NULL` 收窄重派** —— 保留运行时检测，但只对"worker 行已被 prune"的作业重派。**注意：这仍然覆盖不了 S3**（S3 的 worker 行还在），只能算把双跑堵住的正解，不能替代 1。
3. **worker 侧回合超时 + 主动失败** —— 在 worker 执行回合时加 `asyncio.wait_for` 级超时，超时即 finish 作业（失败）+ 释放锁。与 1 互补。
4. **最低限度**：把 `production-ha.md:53` / `microservice-scaling-1000.md:173` 改成"运行时兜底仅监控、**不覆盖 worker 假活**；假活由 `job_timeout` 兜底"，至少让文档不再骗人。

---

## 6. 探针清单与复现命令

| 探针 | 作用 | 命令 |
|---|---|---|
| `.workbuddy-ai/tmp/probe/r1_probe_stall_selfheal.py` | 三场景（S1/S2/S3）自愈覆盖 | 见下 |
| `.workbuddy-ai/tmp/probe/r1_probe_d13_d14.py` | D13 端到端 + D14 容错 | 见下 |
| `.workbuddy-ai/tmp/probe/probe_stalled_double_run.py` | 双跑复现（已翻转） | 见下 |

```bash
cd /Users/timevaeless/Documents/Work/Octop-cluster
# 干净起点
docker exec octop-postgres psql -U octop_admin -d octop_probe_rv \
  -c "TRUNCATE procrastinate_jobs, procrastinate_events, procrastinate_workers RESTART IDENTITY CASCADE;"
# 三场景自愈
TMPDIR=.workbuddy-ai/tmp .venv/bin/python .workbuddy-ai/tmp/probe/r1_probe_stall_selfheal.py
# D13/D14
TMPDIR=.workbuddy-ai/tmp .venv/bin/python .workbuddy-ai/tmp/probe/r1_probe_d13_d14.py
# 双跑（已翻转）
TMPDIR=.workbuddy-ai/tmp .venv/bin/python .workbuddy-ai/tmp/probe/probe_stalled_double_run.py
```

单测：

```bash
mkdir -p .workbuddy-ai/tmp/pytest
TMPDIR=.workbuddy-ai/tmp .venv/bin/python -m pytest \
  tests/unit/cluster tests/unit/agents/test_remote_executor.py tests/unit/agents/test_executor.py \
  -q -p no:cacheprovider --basetemp=.workbuddy-ai/tmp/pytest
# → 49 passed
```

---

## 7. 累计欠账（本提交未覆盖）

| 项 | 内容 | 状态 |
|---|---|---|
| **D2** | 渠道状态不跨节点传播（集群下 IM 渠道闪） | ❌ 未动 |
| **D3** | api 崩溃后回合不可续接 | ❌ 未动（需拍板：续接 vs 降级文档） |
| **D4** | 阶段 0 多副本无门控（每个 `octop run` 都内联跑全部后台单例） | ❌ 未动 |
| **D5** | 控制面迁移无跨进程锁 | ❌ 未动 |
| **D9** | 集群 LB 配不了健康检查 | ❌ 未动 |
| **D6–D8 / D10–D12** | 文档/脚本收尾项 | ❌ 未动 |
| **新增** | **S3 假活线程永久卡死**（本轮 D1 引入） | 🔴 **新增 P1** |
| **新增** | **3 处文档与代码相反** | 🔴 见 §4 |

---

## 8. 诚实边界

- 探针用的是真 PG + 真 procrastinate + 生产类 `AgentWorker`，但**未跑真实 LLM 上游 hang**；S3 是用"作业 `doing` 超时 + worker 心跳刷新"构造的等价形态。要 100% 坐实，需在真实 worker 上制造一次 LLM 不返回的回合，观察线程是否永久卡死。
- 判定 S3 为 P1 而非 P0，前提是"正常回合不会卡住"。若生产环境 LLM 上游/工具偶发 hang 频繁，则应升为 P0。
- `job_timeout` 是否已在他处配置：已 grep `src/`，**零引用**；建议方案 1 前先确认 procrastinate 版本对 `job_timeout` 的支持形态。
