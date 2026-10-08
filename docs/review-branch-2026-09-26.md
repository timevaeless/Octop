# 分支全量代码评审（feat/direct-portal-oidc vs origin/main c04aacc6）

> **修复状态（2026-09-26 更新）**：P0-1、P1-1～P1-10 已全部修复并分五个提交收口
> （`f810d823` P0-1/P1-1/P1-4、`9b5ea0d7` P1-2/P1-3、`2109e63f` P1-5/P1-6、
> `29c10ae1` P1-7~10、`fb66af0e` RLS PG 门控集成测试）；每提交均通过全量单测
> （最终 3403 passed / 5 skipped），RLS 集成测试在 harness octop-postgres 活体验证通过。
> 下方 P2 清单仍未处理；⚠️ 部署提醒：存量 KB/connector 的 org_id 回填在
> `_ensure_resource_acl_schema`（run_migrations 尾部），需容器重建/重启后生效。
> （评审文档含本次修复状态批注，随下次提交一并入库。）

- 日期：2026-09-26
- 范围：`origin/main..HEAD` 全部 24 个提交，146 文件，+17587/−398（55 新增 / 91 修改）
- 方法：四个模块并行深读（mcp_catalog 后端 / dashboard 前端 / 租户化与 RLS / connectors 委派），关键发现经第二人实读代码复核（KB 创建 SQL、paths 消毒、context.py 绑定、delegation.py、019 回填、manager.py 记账均已逐行确认）
- 视角：模块独立防合并冲突、代码健壮性、平台合理性

---

## 一、发现总表（按严重度）

### P0（必须合并前修）

**P0-1【租户数据泄漏】KB 创建全链路漏写 org_id，租户共享 KB 直接变成平台共享**
- `src/octop/infra/db/repos/knowledge.py:141`（`create_base` 无 org_id 参数，INSERT 列清单无 org_id）
- 四张资源表中 agents/published_experts/connectors 创建都传了 `org_id=user.org_id`，唯独 KB 整条链（router→service→repo）没有任何一处传。三段式 RLS 策略与 `shared_resource_visible` 都把 `org_id IS NULL` 定义为 platform-shared ⇒ 租户用户创建 `shared=true` 的 KB 对**所有租户**可见可读，文件落 `shared/knowledge/<kb_id>`。
- 迁移回填也只做了 agents（`_ensure_resource_acl_schema`），KB 无回填。
- 修法：`create_base` 全链加 org_id 并在 router 传 `user.org_id`；补 KB 回填（owner 的 default_org_id）；对 orgless 用户创建共享 KB 显式拒绝。

### P1（合并前应修）

**P1-1【功能阻断】KB 后台索引线程无 tenant scope，PG 下租户 KB 索引必然失败**
- `src/octop/infra/knowledge/jobs.py:69-71`：`loop.run_in_executor(None, process_document, ...)` —— run_in_executor **不拷贝 contextvars**（asyncio.to_thread 才拷贝）。线程内 scope 为 None ⇒ RLS 过滤掉 `org_id` 非空的 KB ⇒ `get_base` 返回 None → LookupError，文档索引永远失败；`resume_pending_index_jobs`（启动期，同样无 scope）重启后全部卡死。SQLite 上 RLS 惰性故单测全绿，恰是「目标方言才炸」的暗雷。
- 修法：`process_document` 开头显式 `bind_tenant_scope(base.org_id)`（base 先经不受 RLS 影响的系统路径取得），或改 `asyncio.to_thread` 并保证入队前已 bind。

**P1-2【租户数据泄漏路径】org_id 允许 `.` 而文件消毒把 `.` 替换为 `_`，两个租户可碰撞到同一目录**
- `src/octop/api/routers/roles.py:24` `_ORG_ID_RE = ^[A-Za-z0-9_.\-]{1,64}$`（允许 `.`）vs `src/octop/infra/utils/paths.py:10` `_safe_segment` 把 `.` 替换为 `_`。管理员创建 org `team.a` 与 `team_b` ⇒ 都解析到 `orgs/team_a/` ⇒ agent 工作区/KB 文件/专家快照混放。
- 前置条件：需要管理员手工建含点号的 org id（当前 org id 以 ULID 为主），但校验规则确实放行。
- 修法：`_ORG_ID_RE` 禁 `.`（或创建时校验 sanitize 后唯一）。

**P1-3【健壮性/纪律】admin bypass 绑定无 reset 配对，防线依赖「每个入口都 resolve」的纪律**
- `src/octop/infra/users/context.py:83-85`：`resolve_authz_context` 对 admin `bind_tenant_scope(org, bypass=True)`，无 token 返回、无 reset。请求路径安全（jwt_auth finally reset），但长生命周期子系统里若存在「先处理过 admin、后续查询未再 bind」的路径，`bypass_org='on'` 残留即跨租户。设计声明 bypass 仅两处显式（migrations/auto_backup），实际 admin 解析是第三处。
- 修法：改为返回 token / 提供 `binding()` 上下文管理器，调用方用完即 reset；grep 盘点所有「resolve 之后隔了 await 再查询」的调用点。

**P1-4【租户数据泄漏】存量 shared 资源升级后 org_id 保持 NULL = platform-shared，跨租户静默放宽且无法归租**
- `019_resource_acl.pg.sql:23` 只回填 agents；connectors、knowledge_bases 无回填。升级瞬间，租户用户既有 shared connector（spec 含业务系统端点/凭据头）立即对全平台可见。且 `_save_custom_servers`（connectors/service.py:292-293）对已存在父行不更新 org——owner 重新保存也无法弥补。
- 修法：新迁移补 connectors/KB 的 owner-default-org 回填；`_save_custom_servers` 在 `row.org_id IS NULL` 时补写。

**P1-5【续签错位】到期记账按 (user_id, server_name) 全局单值，同用户多 agent 场景旧 agent 永久持有过期 token 且无自愈**
- `src/octop/infra/agents/manager.py:1607`：`expiry_store[(user_id, server_name)] = spec_token_exp(spec)` 是全局记账，但 wrapped tools 是 per-agent 挂载。agent A 续签后记账更新 ⇒ agent B 下轮判断「未到期」早退 ⇒ B 仍挂旧 token 连接，过期后 401 且永不再触发续签。
- 修法：记账随挂载实例走（wrapped tool 附加 exp 元数据），或续签不提前更新全局记账、各 agent 按自己挂载的 spec exp 判断。

**P1-6【误判】`spec_token_exp` 不校验 delegate_identity 标志，第三方 IdP 的 OAuth JWT 被当作委派 token 记账**
- `src/octop/infra/connectors/delegation.py:128-150`：只排除非 JWT 的 Bearer。现实中 OAuth access token 多为 JWT ⇒ 静态鉴权服务器被写入记账，进入 margin 后每轮聊天触发一次无效 reload，且永不自愈。
- 修法：记账前先按原始 spec 的 delegate 标志过滤；补回归测试。

**P1-7【配置毒化面】Go 模板注释闸门只查前缀不查闭合，`{{/*x}}`（无 `*/`）整段放行**
- `src/octop/mcp_catalog/router.py:346-347`：`if action.startswith("/*"): continue`。未闭合注释在真实 Go text/template 会报错，闸门却放行——正是 round-6 要防的「坏模板毒化整份 wasmplugins 配置」（fail-static 回退 last-good，风险为静默过期配置）。`PUT /tools` 与 import 路径均可触发。
- 修法：注释分支要求 `endswith("*/")` 且无残余 token；补两条回归用例。

**P1-8【不对称】OpenAPI import 路径对 tool_name 零校验**
- `src/octop/mcp_catalog/router.py:603-610`：manual 路径强制 `_TOOL_NAME_RE`，import 路径 `_snake_case` 产物直接落库。`operationId` 超长可破 64 上限；`/2fa/status` 类路径产出数字开头名字，违反模块自身不变量，进入 ToolAcl 与插件配置。
- 修法：import 循环补 `_TOOL_NAME_RE` 检查，不合格进 `skipped`。

**P1-9【状态损坏】unpublish 无状态前置校验 + 失败回滚无条件置 published，可把 draft 服务翻成假发布态**
- `src/octop/mcp_catalog/router.py:678-706`：对从未发布的 draft 服务 unpublish，若 nacos 侧失败，回滚分支 `update_service(status="published")` 无条件执行 ⇒ 该服务工具进入 `_global_acl` 的 published 查询，随任意服务 publish 被写入全局 ACL。
- 修法：入口 409 拒非 published；回滚恢复进入时原状态。

**P1-10【竞态】发布锁无 fencing：TTL(120s) 可小于 publish 全流程最坏耗时（6 次写 ×20s 超时重试），双发布窗口真实存在**
- `src/octop/mcp_catalog/repo.py:84-123`：接管 CAS 正确，但原持有者租约过期后不被阻止继续写 nacos ⇒ 后写覆盖先写，复现 N1（他人 matchRule 被静默丢）。有 reconcile 兜底故未定 P0。
- 修法：每次 nacos 写前复查 holder 仍为自己（廉价 fencing），或循环内续租。

**P1-11【前端工程】McpCatalog 裸 `validateFields()` 产生未处理 Promise 拒绝，与 Roles 页规范写法不一致**
- `ToolsDrawer.tsx:127`（`await form.validateFields()` 无 try/catch，`onOk={() => void saveTool()}`）——每次校验失败控制台 Uncaught。Roles 页（PackagesTab/OrgsTab）是 try/catch 早返回的正确范式。
- 修法：对齐 Roles 页模式；顺带统一 `destroyOnClose`→`destroyOnHidden`（3 处 deprecation warning）。

### P2（改进项，按模块归组）

**mcp_catalog**
- publish/unpublish 异常捕获不对称（publish 漏 KeyError/TypeError，失败不写 publish log，审计断档）——router.py:660 vs 701。
- published 服务 PATCH 网关字段（mcp_path/backend_port 等）不触发重发布，DB 与网关漂移无提示——router.py:448-472。
- nacos 404 判定靠错误消息字符串匹配（`"http 404" in str(e)`），重构即静默破坏——nacos_client.py:126,168；401/403 重登无测试覆盖。
- env 命名偏离仓库 `OCTOP_*` 约定（用 `MCP_CATALOG_NACOS_*`）+ 默认弱口令 nacos/nacos + import 时固化读取——router.py:41-43。
- OpenAPI 拉取无响应大小上限——router.py:571-574。
- `build_plugin_tool` 的 location→position 翻译实际未删 location 且 `p.pop` 变异 frozen dataclass 的可变成员——publisher.py:69-72。
- 杂项：create_service check-then-insert 并发 500 而非 409；publish_log 无保留策略（内嵌全量 YAML/ACL）；allowed_roles 无角色存在性校验（拼错=静默无人可用）；`_HTTP_PATH_RE` 允许 `..` 段。

**dashboard**
- 行内启停用整包 PUT 回写且 Switch 无 loading 防抖，快速连点后写覆盖先写——ToolsDrawer.tsx:170-189。
- 错误 fallback 复用 `mcpCatalog.loadFailed`（key 名与语义错位，Roles 页区分了语义位）——index.tsx 多处。
- routes/prefetch.ts 漏登记 `/admin/mcp-catalog`（routes/sidebarNav/permissions 三处都有，唯独 prefetch 缺）。
- `layout.css` 滚动条 thumb 硬编码 `rgba(0,0,0,0.18)`，违背「只用 --fn-* 色变量」约定——styles/layout.css:517-531。
- 默认布局 classic→minimal 的行为变更混在「质感美化」提交里，回滚样式时易被误回滚——layoutModeStorage.ts:18。
- MembershipTab 每页 N 个逐用户请求（注释自知），用户量上来后需批量端点。

**租户化/RLS**
- 「查一行拿 org_id」模式广泛 try/except 吞异常 ⇒ 未绑定上下文中解析到 `shared/`，同一资源数据分裂两处（agent workspace_dir 甚至持久化错误路径）——agent_workspace.py:36-45 等四处。
- 025 的 `CREATE POLICY` 不可重放（PG 无 OR REPLACE），建议头部加 `DROP POLICY IF EXISTS`——025_rls.pg.sql。
- org 改绑后旧目录成孤儿（用户视角文档消失）；restore 侧对包内 orgs/<name> 未见消毒（需核实 `_replace_tree_from_archive`）。
- JIT 重映射新 org 的能力包存了但永不生效（default_org_id 非空不更新，role_keys 只查 home org）——语义取舍需写进设计文档。
- 登录/JIT 链路 audit 写失败会阻断登录（管理面同事务是合理取舍，登录链应 try/except）——audit.py:57-62。
- CLI offline_ops 无 scope（RLS 下删不了租户 agent）；websocket 入口不经过 http middleware，WS 链路调用点需盘点。
- slug 全局唯一 vs RLS：租户用户查重漏判他租户行 ⇒ INSERT 撞唯一索引 500——改 bypass 查重窗口或 `(org_id, slug)` 复合索引。
- 部署检查项：应用池误用 superuser/BYPASSRLS 角色会使 RLS 全失效，建议启动健康检查断言 `rolsuper/rolbypassrls`。

**connectors/委派**
- 并发续签竞态：两 turn 各自 mint（iat 不同⇒fingerprint 不同），stale 清理删掉刚写的缓存，双倍 MCP 会话建立。
- TTL env：负数/0 静默钳到 10s、无上限、无日志——delegation.py:45-52。
- `verify_internal_token` 非常数时间比较；per-instance token 走 URL query 会进访问日志（main 既有，本分支扩大调用面）——service.py:664-669。
- GET/DELETE internal 端点不校验 status==active，与 POST 不一致——internal_mcp.py:108,144。
- `decrypt_credentials` 对 InvalidToken 不设防，单个损坏 blob 使所有用户列表整体失败（shared-parent 循环内裸调）——crypto.py:25-28。
- 时钟回拨/跨机偏移无防护（无 nbf/leeway 约定）；hub 侧 requireIssuer 仅注释层面，无自检无集成测试。
- orgless 非 admin 用户 shared=true 落 NULL=platform-shared，共享外溢无门禁——connectors.py:715-721。
- 迁移注释漂移：019/018 注释仍引用旧编号 016/017。

---

## 二、合并冲突风险矩阵（合上游时的热点）

| 文件 | 侵入深度 | 说明 |
|---|---|---|
| `infra/agents/manager.py` | **高** | +172 行散布 8+ 方法（MCP 续签/delegation/workspace/authz 线程化），与上游 MCP 热区重叠最大 |
| `infra/db/migrate.py` | **高** | +313 行（018-023 分支、常量表、_ensure_* helper）；上游每版迁移必改此文件，尾部调用链高频冲突 |
| `infra/knowledge/service.py` | 中 | 6 方法 ACL 改写 + 新增共享判定 |
| `infra/db/repos/{agents,knowledge,published_experts,connectors}.py` | 中 | 行数据类加字段 + list 重写为 Python 过滤 |
| `infra/db/pool.py` / `api/middleware/jwt_auth.py` | 中/低 | connect/transaction 各插 6 行 scope 读取；middleware 15 行 try/finally |
| `locales/zh.json` / `en.json` | 中 | 各 +349 行**纯追加**（328 新 key，0 删除 0 改值，未触碰历史重复键）；rebase 当独立步骤处理 |
| `App.tsx` / `index.html` / `styles/theme-vars.css` | 中 | themeConfig 重构 + xhs 皮肤 |
| `layouts/sidebarNav.tsx` | 低-中 | +20 行登记（nav 是热点区） |
| `api/app.py`（mcp_catalog 挂载） | 低 | 唯一真实侵入点约 4 行 |
| `routes/`、`permissions`、`api/modules/auth.ts` | 低 | 纯登记性追加 |
| 新增文件（mcp_catalog/ 模块、Roles 页、McpCatalog 页、migrations 018-025、tenancy_context.py、delegation.py 等） | 无 | 自包含，零冲突面 |

亮点：mcp_catalog 模块对 db/services.py、pool.py、deps.py **零侵入**；Roles 页与 McpCatalog 页均为新建自包含；模块隔离纪律执行到位。

---

## 三、已验证为正确的关键点（正面结论）

- **bypass 无 HTTP 注入面**：全仓 `bypass=True` 仅四处（jwt_auth is_admin、resolve_authz_context is_admin、run_migrations、run_auto_backup），无请求参数可置位。
- **SET LOCAL 生命周期正确**：`set_config(..., is_local=true)` 随 checkout 事务消亡，池化连接无残留；中途 commit 只丢绑定（fail-closed 方向）。
- **迁移原子性**：PG 每版本单事务，025 半途失败整体回滚；020-023 幂等通道（IF NOT EXISTS / _ensure_column）设计正确。
- **审计租户语义与设计表一致**（target org / actor org / NULL 三类各就位）。
- **委派铸造 fail-closed**：铸造失败返回 None 不加头，hub 401 兜底；token 不入日志不入响应；claim 无 role 旁路。
- **前端质量高**：两个新页 0 处 any、0 处 JSON.parse（无前端解析面）、权限位 admin-only 无漏、328 新 key 双语 parity 完整、useAsyncData 防过期覆盖。
- **mcp_catalog 分层清晰**：router 校验 / repo 纯 SQL / publisher 纯函数 / nacos_client 薄封装，9 轮评审的历史坑均有回归测试锚定。

---

## 四、总体判断与修复顺序建议

1. **必须修（合并前）**：P0-1（KB 漏 org_id）→ P1-1（索引线程无 scope，PG 下 KB 核心功能不可用）→ P1-4（存量 shared 资源回填）——三者同属「绑定纪律未覆盖所有创建/后台路径」，建议一次提交收口。
2. **应修**：P1-2（org_id 禁点号）、P1-3（bypass reset 配对）、P1-5/6（续签记账）、P1-7/8/9（mcp_catalog 三处确定性缺陷）。
3. **建议补**：RLS 层 PG 门控集成测试（双租户互不可见 / 无 scope 只见 NULL / checkout 清除 / FORCE 对 owner 生效）——当前整层 RLS 零 PG 级测试。
4. **合上游前预演 rebase**：manager.py 与 migrate.py 是两个高危撞点，建议先行 rebase 预演；locales 两个文件当独立步骤处理。
