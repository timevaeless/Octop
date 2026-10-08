# 分支全量代码评审（第二轮）：模块独立性与社区代码污染专项

- 日期：2026-09-30
- 范围：`git diff 232030f4..HEAD`（232030f4 = v1.0.2b4，与 origin/main e473dd3c 的共同祖先；
  本地 main c04aacc6 已陈旧，不作为基线）
- 体量：253 文件，+28517/−701（114 新增 / 137 修改上游文件）
- 视角：T4 改造约定（09-26 拍板）——「改造抽独立模块可插拔，上游文件只留一行懒加载
  hook，拔掉即恢复原版」；围绕模块化纪律与上游合并污染面
- 上轮评审（docs/review-branch-2026-09-26.md）：P0-1、P1-1~P1-10 已收口，P2 清单仍开放

---

## 一、总体结论

**纪律执行整体到位，未发现污染性回退。** 四个量化事实支撑：

| 维度 | 事实 |
|---|---|
| 新功能隔离度 | mcp_catalog 后端 9 文件、Admin/McpCatalog 5 页、Admin/Roles 11 页、infra/users 10 模块、migrations 30 个——**全部为新增文件**，不与上游文件交叠 |
| hook 合规度 | 抽查 6 处 hook 全部合格（见下），均带「T1/T2/T3 pluggable hook」声明与拔除说明 |
| 上游文件净瘦身 | users/manager.py −76/+117、migrate.py 330 行 ensure_* 抽出至 tenancy_schema.py——方向是抽出而非织入 |
| 测试伴随度 | 新增前端逻辑均配 .test.ts（sidebarNav/layoutModeStorage/bootTheme/chatStreamError），单测基线 3614 passed |

### 抽查合格的 hook（每处均为懒加载单行委托 + 拔除声明）

1. `users/manager.py:_apply_jit_resolution` → `users/tenant_hydration.apply_jit_resolution`（T1-1）
2. `agents/manager.py:_mcp_delegation_expiring` → `agents/mcp_token_refresh.delegation_expiring`（T3，含 P1-5 per-agent 修正）
3. `agents/manager.py:_replace_server_mcp_tools` → 同上模块
4. `api/app.py:install_ws_tenant_scope` → `api/middleware/ws_tenant_scope.install`（WS RLS 作用域）
5. `db/repos/{agents,knowledge,connectors,published_experts}.py:_parse_role_list` → `users/resource_acl.parse_role_list`（T2，消除四份逐字拷贝）
6. `db/migrate.py` → `db/tenancy_schema.ensure_*`（四个 ensure 函数全部委托）

---

## 二、发现（按严重度）

### P1-1【污染热点】agents/manager.py +236/−13 —— 全分支最大织入面

可见性过滤方法族（`visible_knowledge_bases` / `assignable_knowledge_bases` /
`visible_mcp_servers` / `assignable_mcp_servers`，约 90 行）与 `prepare_chat_mcp`
的 custom/builtin 分流扩展直接长在上游 `AgentManager` 类体内。T3 委派记账已 hook 化，
但可见性族没有。上游目前未改此文件（不在冲突重叠清单），风险是未来性的：
上游一旦重构 manager，此处是最大冲突源。

**建议**：抽 `octop/infra/agents/visibility.py`（或 mixin），manager 留单行委托，
与 mcp_token_refresh 同款。属可选优化，非合并阻塞。

### P1-2【随身补丁】docker/Dockerfile 构建定义修改

去掉 `# syntax=docker/dockerfile:1` 指令（国内网络 BuildKit 解析超时）+
`--extra local-embedding`。有注释说明理由，但这是**每次同步上游必然冲突**的文件，
且环境特异性最强。建议：在 docs/my/ 维护「随身补丁清单」（Dockerfile、
pyproject ruff ignore、.gitignore），同步上游时按清单核对，避免静默丢失。

### P2-3【二次抽取项】connectors/service.py +116/−15

authz 签名扩展与共享视图展开（`_shared_custom_parent_visible` 调用链）织入方法体；
`_delegated_identity_token_for` 定义在 service.py 而非 delegation.py 模块。
功能正确（共享 spec 携带 owner 端点/凭据，可见性判定必要性成立），建议后续把
token 铸造挪进 delegation.py、共享展开逻辑抽独立函数。

### P2-4【固有织入】归属三态的列管道

repos/agents.py +96、knowledge.py +90、users.py +54、connectors repo +69 等——
org_id 列在 CRUD 各环节的贯穿是归属重构的必需成本，无法 hook 化（SQL 列清单本身就是
上游代码）。解析类逻辑已通过 `_parse_role_list` 四连 hook 收口。**维持现状，备忘即可。**

### P2-5【双源风险】dashboard/index.html 启动脚本调色板 ~40 行

boot 脚本无模块化能力，织入可接受；但 `PALETTES` 数组与 `themePalettes.ts` 构成双源
（注释已声明 mirror 关系，bootTheme.test.ts 有一致性测试）。低风险，保持测试覆盖即可。

### P2-6【i18n 地雷延续】zh/en.json 各 +472 行

历史重复键问题仍在（round-trip 断言因此不可用），新增 key 一直靠外科手术式插入维持。
**两个 locale 文件都在上游冲突重叠清单内**——下次同步上游若大改 locale，需人工逐块核对。

### P2-7【迁移编号占用】018–033 已占用上游命名空间

已知上游撞号史（018/019/020）。`_reconcile_pre_squash_schema_version` 兜底机制在位，
同步上游新迁移时仍需人工核对编号，维持现有纪律。

---

## 三、上游冲突风险清单（上游 e473dd3c 已动 ∩ 我方也动过）

上游 self 232030f4 之后继续演进（e473dd3c），以下 ~30 个文件两侧都改过，下次同步
重点核对。后端 8 个：

- `src/octop/api/app.py`（hook 挂载，冲突概率低）
- `src/octop/infra/db/migrate.py`（hook 化后冲突面已收窄）
- `src/octop/infra/db/services.py`（+8 行接线）
- `src/octop/infra/gateway/process/processor.py`（39 行，含 09-30 降级逻辑——**重点**）
- `src/octop/infra/server.py`、`src/octop/infra/users/preferences.py`（2 个 nav key）

其余为前端 chat/layout/locale 文件与测试，冲突处理成本低。

---

## 四、结论

1. 「独立模块化、不污染社区代码」的约定自 09-26 以来执行良好，本轮新增代码
   （MCP 目录全链、专家模板 defaults、对话降级）均落在独立模块或以 hook 接入；
2. 无阻塞项。P1-1（manager 可见性族抽取）与 P1-2（随身补丁清单）建议排期处理；
3. 上轮 P2 清单继续开放（publish 异常对称性、nacos 404 字符串匹配等）。
