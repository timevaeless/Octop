# 可插拔改造审计：分支 vs main 全量分类（2026-09-26）

> 用户约定（2026-09-26 拍板）：改造尽量抽成单独模块、可快速插拔、不影响社区原版功能——
> 新逻辑进独立新文件；上游文件只留一行懒加载 hook + 注释指回模块；repo 只加 additive
> 方法；**拔掉模块 + hook 即逐字恢复社区行为**。
> 范围：`main...feat/direct-portal-oidc`（merge-base c04aacc6），164 文件 / 69 新文件。
> 审计方式：三路并行探查逐文件核对 + 主线复核。改造执行：批次1 `a504e6a2`、批次2（本文档同日提交）。

## 一、新模块清单（拔除 = 删文件 + 删 hook）

| 模块 | 行数 | 职责 | 上游 hook 形态 |
|---|---|---|---|
| `infra/users/org_resolution.py` | 298 | 五级归位解析（决策） | manager 委派一行 |
| `infra/users/tenant_hydration.py` ★新 | ~135 | JIT 结果持久化编排 + User 水合 role_keys | `_apply_jit_resolution` 委派 + `_user_from_row` 分支 |
| `infra/users/lifecycle.py` | 119 | token_epoch 闭环 + pending 留痕 | 4 个一行 hook（T4-3） |
| `infra/users/invite_tenancy.py` | 109 | 邀请绑租户（T4-1） | 3 个一行 hook |
| `infra/users/token_tenancy.py` ★新 | ~150 | JWT 租户 claims 签发 / ACL 判定 / ver 门禁 | deps 两个委派 + resolve 一行 |
| `infra/users/resource_acl.py` ★新 | ~100 | ACL 谓词收敛（4 份重复 `_parse_role_list` + `roles_allow` + `visible_shared_row`） | repo 循环一行化；`_base.py` **逐字还原** |
| `infra/users/tenancy.py` / `tenancy_context.py` / `context.py` | 78/105/121 | 规则源 / RLS scope / authz 窗口 | 本身即模块 |
| `infra/db/tenancy_schema.py` ★新 | ~330 | 租户/ACL schema 引导（原 migrate.py 织入 345 行） | migrate 保留版本分支 + 尾部 4 调用 + re-export |
| `infra/utils/tenant_paths.py` ★新 | 131 | 租户文件布局 mixin（原 paths.py 织入 ~100 行） | paths.py 保留 import+mixin 基类两行 |
| `api/common/agent_tenancy.py` ★新 | ~80 | by-id 共享 agent 租户门禁 | require_agent_row/agents.py 各一行 |
| `api/common/agent_workspace_tenancy.py` ★新 | ~70 | 默认布局租户分支 | 3 行委派 |
| `api/common/expert_tenancy.py` ★新 | ~105 | 发布模板五处租户/ACL 逻辑 | 5 个 hook 点 |
| `api/routers/pending_users.py` | 119 | 待归位管理面（T5-4） | app.py 一行注册 |
| `infra/agents/mcp_token_refresh.py` ★新 | ~95 | 委派 token 过期判定 + 工具换装 | 两个委派方法（状态字典留在 manager，同 `_mcp_tool_cache` 形态） |
| `infra/backup/tenant_layout.py` ★新 | ~95 | 备份的 shared+orgs 知识树 | create/restore 各一个 hook 块 |
| `api/common/…` 其余（delegation/mcp_catalog/connectors ACL 等） | — | T3 系列 | 此前已合规 |

★ = 本次改造新增。

## 二、上游文件残余 delta 分类（改造后）

- **HOOKED（一行/几行懒加载）**：app.py、users.py、invites.py×2、jwt_auth.py、pool.py×2、
  deps.py×3、agent.py、agent_workspace.py、experts.py×5、manager.py(users)×2、
  manager.py(agents)×2、service.py(connectors)×2、system_archive.py×2、4 个 repo 的循环行。
- **PARAM（纯 additive 签名/列扩展，抽无可抽，允许保留）**：identity.py（User 字段）、
  audit.py、sso.py、users.py repo（bump_* 新方法）、knowledge/files.py、workspace_dir.py、
  builder.py、AgentCreateSpec.org_id、各路由 org_id 透传、`_published_summary_dict` 两个键、
  `PublishExpertBody.allowed_roles`、agents/manager 的 `resolve_authz_context` 透传。
- **恢复逐字原样**：`repos/_base.py`（diff 为空）、`deps.sign_token`（社区原样）、
  `paths.py` 三个 legacy 属性 docstring 还原。

## 三、有意保留的耦合（不抽的理由）

1. **数据层 org 列过滤**：repo 查询里的 `WHERE org_id`/INSERT org_id 是参数化扩展（PARAM），
   不存在"拔掉后仍要跑"的社区路径——拔除以 schema（tenancy_schema）为单位。
2. **migrate.py 的版本分支与 bypass 包装**：版本推进与迁移器骨架属上游文件本体，hook 保留
   调用行；run_migrations 的 RLS bypass 包装（~7 行懒加载）包裹的是迁移器自身。
3. **agents/manager `_mcp_token_expiry` 字典**：与社区 `_mcp_tool_cache` 同形态的实例状态；
   决策逻辑已抽走，仅存状态声明与 invalidate 清理（~10 行）。
4. **dashboard 前端改动**（theme/layout/locales/routes）：UI 层无"社区原版可拔"语义，
   页面级模块（PendingTab 等）删除注册行即恢复。
5. **测试文件适配**：签名变更带来的测试更新是改造伴随成本，不算织入。

## 四、拔除手册（快速回滚社区原版）

1. 删除 §一 表中 ★/新增模块 + 上述 HOOKED 行（每处都有 `pluggable hook` 注释可 grep）。
2. `grep -rn "pluggable hook" src/octop` 列出全部触点（截至本文档：约 35 处）。
3. 数据库：`_schema_version` 降回 15 并重建库（迁移 016+ 为租户 plane；见 MEMORY 活体降级法）。
4. 前端：删除 pending/orgs 相关注册行。
