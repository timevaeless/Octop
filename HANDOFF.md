# HANDOFF — cluster 阶段收尾与遗留缺陷修复

> 交接时间：2026-09-25 ｜ 工作目录：`/Users/timevaeless/Documents/Work/Octop-cluster`（git worktree）
> 分支：`feat/cluster-stage3`（基点 `dc07c650`，纯 cluster 提交链，HEAD `e603705f`）

---

## 1. 当前布局（为什么要在这里干活）

| 位置 | 分支 | 状态 |
|---|---|---|
| `/Users/timevaeless/Documents/Work/Octop` | `feat/direct-portal-oidc` | **cluster 提交已全部移除**（历史重写，`dc07c650..HEAD` 仅剩 mcp 提交 + 1 个 chore 补偿提交）。有其他开发的未提交 WIP，**不要动**。 |
| `/Users/timevaeless/Documents/Work/Octop-cluster`（本目录） | `feat/cluster-stage3` | cluster 改造的全部代码（6 个提交），后续 cluster 缺陷修复**只在这里做**。 |

- 两侧基点相同，文件不相交（cluster 只碰 `infra/cluster/**`、`infra/agents/remote.py`、`infra/agents/executor.py`、`server.py` 接线、`api/routers/chat/ws.py`、gateway 取消路由、`cli/commands/agent_worker.py`、`docker/`、`scripts/stage3/`、`docs/deployment-stage3.md` 等）。
- 最终通过正常 merge 汇合；保持 server.py 等社区文件足迹最小（当前仅 2 行 gate 调用）。
- 注意：两侧分支都经历过历史重写，**SHA 已变**。cluster 链新 SHA：`427b1ff2 → 1a2c972a → 14be3500 → 7e19d1ca → 61ed6302 → e603705f`。

## 2. 评审报告（修复依据）

`docs/cluster-review-2026-09-25.md`（已提交在本分支 `e603705f`）。

- 第一轮 P0-1/P1-2/P1-3/P1-4：**已修复并实测确认**（提交 `61ed6302`），配套回归测试在 `tests/unit/cluster/`、`tests/unit/agents/test_remote_executor.py`。
- 仍遗留：下表 10 项，全部核实过在当前代码中**确实存在**。

## 3. 待修复缺陷（按优先级）

| # | 级别 | 问题 | 位置与修法方向 |
|---|---|---|---|
| **R2-1** | 🔴 | 阶段 0「N 个完整实例 + LB」形态无门控：local 模式 `BackgroundGate(None, enabled=True)` = 单机语义，N 副本全跑后台单例 | `src/octop/infra/server.py`（`leader_runner` 仅在 `execution_mode=="remote"` 时创建）、`src/octop/launch.py:74`。给 local 多副本加显式门控（如 `OCTOP_LEADER=1` 环境变量或让 gate 感知多副本配置） |
| **R2-2** | 🔴 | 控制面 `run_migrations` 无跨进程互斥；`--scale` 并发启动会撞重复对象，`_ensure_published_experts_schema` 并发重建更危险 | `src/octop/infra/db/migrate.py` 全程无锁。对标 `remote.py` 的 `ensure_queue_schema`（`pg_advisory_lock` 串行化）补同样机制 |
| **§8.2** | 🟠 | 修复引入的边界问题：runtime 检测器对 `doing` >2100s 的作业无条件 `retry_job`，worker 存活时误重派 → 同 job 双跑 | `src/octop/infra/agents/remote.py` `retry_stalled_jobs_once()`（`TURN_JOB_MAX_RUNTIME_SECONDS=2100`，约 :1150-1166）。收窄：对 worker 仍存活（心跳未断）的作业不做 runtime 兜底，只保留 heartbeat 检测器重派；或调大阈值 |
| **R2-3** | 🟠 | 文档给的启动命令参数不存在 | `docs/deployment-stage2.md:37,51` `octop agent-worker --workspace` → CLI 实际只有 `--concurrency/--debug`。改文档按实际参数写，workspace 靠环境变量 |
| **R2-4** | 🟠 | 文档承诺「worker 启动时预热」与实现（`prestart_agents` 机制）描述未对齐 | `docs/deployment-stage2.md:75`，同步 `docs/deployment-stage3.md` 措辞 |
| **R2-5** | 🟡 | IM 渠道状态是节点内内存态，follower 返回 `standby/not leader`，经 LB 轮询前端状态来回闪 | `src/octop/infra/gateway/gateway.py:666-667`（`_set_runtime_status`）。状态落库或让该接口统一从 leader 取 |
| **R2-6** | 🟡 | 集群 LB 用变量 + resolver，无 `upstream` 块，配不了 `max_fails`/`proxy_next_upstream`；挂一个 api 后 1/N 请求 502 | `docker/cluster-lb.conf`。文档（production-ha.md:21 等）宣称的 LB 健康检查要能落地 |
| **R2-7** | 🟡 | 分层违规：`infra/` 反向 import `api/`，违反 AGENTS.md §5 硬禁令 | `src/octop/infra/agents/remote.py:103` `from octop.api.routers.chat.sse import json_chunk_default`。把该纯函数下沉到 `infra/utils/`（或同级公共位），api 侧反向引用 |
| **R2-8** | 🔵 | 5 个压测/证据脚本硬编码本机沙箱 Python 路径 | `scripts/{poc1,poc2,poc3,stage2,stage3}/run_*.py`。改 `sys.executable` 或环境变量；result.json 绝对路径脱敏 |
| **R2-9** | 🔵 | 三个健壮性缺口：① `TurnEventPublisher.publish` 仅在新 chunk 到达且距上次 ≥50ms 才落库，长静默期间最后 token 滞留（`remote.py:812-817`）；② `TurnStreamReader` SELECT 无 `LIMIT`（`remote.py:459-460`）；③ 队列无启动预热，首回合承担建表+App 初始化 | ① 加定时 flush（或 publish 后安排延迟 flush）；② 加 `LIMIT`（配合 seq 游标分页）；③ boot 时预调用一次 `_ensure_open` |

**建议顺序**：R2-1 → R2-2 → §8.2 → R2-3/4（文档收尾）→ R2-7 → R2-5/6 → R2-9 → R2-8。

## 4. 修复纪律

1. **每修一项配针对性回归测试**（对齐第三轮风格：`test_worker_role_starts_no_background_singletons` 这类几行断言就能挡住整类回归；文档承诺尽量转成可执行断言）。
2. **新逻辑继续收敛进 `octop.infra.cluster` 包**，社区共享文件（`server.py`、`ws.py`、`gateway.py`）足迹保持最小，为后续与社区代码合并减冲突。
3. **文档改完要过一遍三份交叉对照**：`production-ha.md` / `deployment-stage3.md` / `microservice-scaling-1000.md`，别再引入 P3-9 式矛盾。
4. 架构实质变更后，按约定在 `docs/architecture-evolution.md` **追加**一张 ASCII 图（★ 标变化点，注版本代号/日期）。

## 5. 环境与验证命令

```bash
cd /Users/timevaeless/Documents/Work/Octop-cluster

# 测试（venv 已建好；必须去掉 PYTHONPATH，WorkBuddy shim 会拦 mkdir/rmtree 炸掉 xdist）
env -u PYTHONPATH ~/.local/bin/uv run --frozen pytest tests/unit/cluster tests/unit/agents/test_remote_executor.py -q

# 全量质量门（lint + mypy + pytest，当前全绿基线：46 cluster 相关单测通过）
env -u PYTHONPATH ~/.local/bin/uv run --frozen make all

# pytest 需要临时目录参数时指向工作区内（.gitignore 已含 .workbuddy.pytest-tmp*）
```

- Python/Node 用 `~/.workbuddy/binaries/` 下的托管版本；uv 在 `~/.local/bin/uv`。
- 真 PG 容器名 `octop-postgres`，可复现评审探针（`.workbuddy/cluster-review-evidence/` 有第一轮全部复现脚本）。
- cluster 单测基线：`tests/unit/cluster` + `test_remote_executor.py` + `test_executor.py` = 46 passed。

## 6. 主目录备忘（勿动区）

- `feat/direct-portal-oidc` 上的 chore 提交 `a58c8451` 是从 cluster 提交里剥离出的非 cluster 修复（lint 格式化/类型注解/测试环境无关化/B008 豁免），merge 回主线时不要丢。
- 该分支还有一个**其他开发引入的既有 mypy 错**已顺手修掉（`manager.py:1490`，`bool()` 包裹），包含在同一 chore 提交内。
- 主目录工作树有其他开发的 WIP（dashboard/connectors/mcp 约 30 个文件），**任何 cluster 操作不要在主目录做**。
