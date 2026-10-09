# 奇瑞海外汽车金融项目（summer-cloud）种子工程约定

> **示例工程约定**：本文件随「全栈工程师·数字员工」预设携带，作为首个工程的种子知识。
> 权威源以仓库为准（`rules/`、`docs/`、`AGENTS.md`、`CLAUDE.md`），本文件只做索引与要点。
> 换工程时按记忆协议（SKILL.md 第 8 节）为新工程发现约定并创建「工程约定-{工程名}」文档，不沿用本文件。

## 一、工程概览

- 奇瑞汽金（马来西亚）汽车金融微服务工程，基于 RuoYi-Vue-Pro 二次开发。
- 根 `pom.xml` 聚合通用底座（summer-framework、summer-gateway、summer-module-*）与业务工程（summer-cheryfs-*）；`summer-ui` 为前端工程。
- 技术栈：Java 17 / Spring Boot 3.5.9 / Spring Cloud 2025.0.0 / Nacos / MyBatis-Plus 3.5.15 / MySQL / Redis+Redisson / RocketMQ / Feign / XXL-Job+SchedulerX / Flowable 7.2.0 / Knife4j / SkyWalking / Lock4j / FastExcel / JSch(SFTP) / MapStruct / Hutool / Jasypt；前端 Vue 3.5 + Element Plus + TS + Vite + pnpm。

## 二、模块职责

| 模块 | 职责 |
| --- | --- |
| summer-cheryfs-laac | 进件审批：零售贷款全生命周期（申请、BPM 审批、合同、放款、任务编排） |
| summer-cheryfs-h2h | 银企直连：SFTP 批量代付/批扣/流水对账，多银行网关抽象，Outbox 模式 |
| summer-cheryfs-cmc | 客户管理：经销商/客户档案、KYC |
| summer-cheryfs-cxk | 车型库（贷款抵押物） |
| summer-cheryfs-cl | 信用评分 |
| summer-cheryfs-dlp | 影像系统 |
| summer-cheryfs-pmc | 产品中心：金融产品配置、补贴/贴息政策 |
| summer-cheryfs-plc | 脚手架占位 |

## 三、DDD 分层约定（所有 summer-cheryfs-* 通用）

每个业务工程为 Maven 聚合工程，包根 `cn.summer.<域>`，含 `start/adapter/app/domain/infra/client/api/common` 八层；`domain/<子域>` 分 entity/enums/valueobject/repository/query；`infra/<子域>` 分 dataobject/mapper/repository/convert（MapStruct）；Job 放 `adapter/job/`（XXL-Job）与 `adapter/job/schedulerx/`。

## 四、工程规范权威源（rules/，开发前必读）

- `rules/summer-cloud-principles.mdc`：工程规范与 DDD 开发顺序（先 domain 模型/状态机/幂等/一致性，再实现；中文回复）
- `rules/h2h-principles.mdc`：H2H 准则（不重复扣款/不重复放款、可追溯、可回滚、可扩展多银行；跨系统资金动作先幂等一致性；业务逻辑不进 Controller/Repository）
- `rules/ponytail.mdc`：最简实现（YAGNI、复用既有工具/标准库）
- `rules/work-report-principles.mdc`：工作汇报与发版清单自动汇总约定
- 与 `.cursor/rules/` 同步，修改时两处一致。

## 五、交付物规范与模板路径

- 规范：`docs/交付物管理规范.md`（一个需求一个目录；详设必须在编码前完成归档；闭环同产 代码评审/单元测试/联调测试/上线及回退步骤）
- 模板：`docs/技术交付/` 下 详细设计报告-模板 / 代码评审报告-模板 / 单元测试报告-模板 / 联调测试报告-模板 / 上线及回退步骤-模板；`docs/项目管理/需求排期-模板.md`；`docs/产品设计/产品设计说明书-模板.md`
- 归档：`docs/技术交付/{需求名}/`、`docs/项目管理/{需求名}/需求排期.md`、`docs/产品设计/{需求名}/`
- 数字员工机制（流程蓝图/签核台账/知识卡）：`docs/数字员工/`（见第十二节）

## 六、测试用例约定

- 模块级测试用例：`summer-cheryfs-{域}/docs/*-test-cases.md`（如 `summer-cheryfs-laac/docs/laac-test-cases.md`：核心不变量表 + 用例编号 `{模块}-{阶段}-{序号}` + 优先级 P0/P1/P2 + 场景/预期/关联测试）
- JUnit + `@ExtendWith(MockitoExtension.class)` + AssertJ；场景编号与代码注释 `【模块-*】` 对齐
- 单测报告覆盖率目标：类 ≥80%、方法 ≥80%、行 ≥70%

## 七、工作台账与 AI 提效评估

- 台账：`docs/项目管理/工作台账-{YYYY}-W{ISO周}.md`（ISO 自然周；六大板块：已闭环/进行中/重点事项/AI提效专项/团队管理/其他）
- 汇报人：芮阳阳｜所属项目/模块：海外项目组
- 命名：【需求】/【问题】/【优化】「关于 XXX 的…」；一个需求一条，子项合并进备注
- **汇报语言禁实现细节**（表名/字段/SQL/类名/框架术语），用业务语言
- 提效评估：效率 = 客观评估人天 × 8h ÷ 实际 AI 时长；倍率保守 1.4~1.5；台账量化列写 `人工评估 X 人天 / AI 实际约 Y 小时 / 提效约 Z 倍`

## 八、发版清单

- 规范：`docs/发版清单/发版清单规范.md`；用户说「生成本周发版清单」时，git fetch 后汇总该周**已推送到远程**的功能分支（不论是否合入主干），写 `docs/发版清单/发版清单-{YYYY}-W{ISO周}.md`
- 不代替各需求 `docs/技术交付/{需求名}/上线步骤.md`

## 九、H2H 要点（summer-cheryfs-h2h）

- 业务域：payment（代付/放款）、collection（批扣）、tx（MT940/MT942 对账）、mandate（代扣授权）、balance、accounting
- 核心机制：Outbox 模式下发与回执（OutboxPaymentServiceImpl、OutboxCollection*、OutboxTxDtl*）、文件批次拉取/去重（fileHash 优先）、`GatewayRouter` 按渠道配置路由银行适配器
- 银行适配：`infra/gateway/bank/cimb/`（真实）+ `infra/gateway/bank/stub/`（本地联调桩，开关 `summer.h2h.bank-stub.enabled`）；文件解析支持固定宽度文本、SWIFT MT940/MT942、PGP 加密

## 十、i18n 多语言约定

- `summer-ui/src/locales/{zh-CN,en,ms}.ts` 三语并行（中文/英文/马来语）；新增文案三语同步
- 前端 `src/api/<模块>/` 与 `src/views/<模块>/` 按模块一一对应

## 十一、关键文档

- `docs/00-整体架构设计.md`、`docs/0X-summer-cheryfs-<域>-详设.md`（01-pmc/02-laac/03-h2h/04-cmc/05-cxk/06-cl/07-dlp）
- `docs/LAAC-代码规范速查.md`、`docs/操作手册-详细版.md`、`summer-cheryfs-h2h/README-bank-stub.md`

## 十二、数字员工知识沉淀机制（docs/数字员工/）

- 运行流程：按预设阶段签核制（七节点，口头签核，不强制额外留痕），不做流程细分
- 沉淀总览：`docs/数字员工/README.md`（硬规则：**需求知识卡闭环必产（缺卡不算闭环）、分层沉淀、开工先检索、每 5~10 个任务模式提炼需用户确认**）
- 需求知识卡：任务闭环强制产出 `docs/数字员工/知识卡/{任务名}.md`（模板：`docs/数字员工/需求知识卡-模板.md`，含 业务诉求/规则/接口契约/决策/踩坑/工作量/复用记录），同步更新**本工程**知识库（海外汽车金融-工程约定）分类分节
- **沉淀归并（本地）**：闭环时将知识卡精炼要点（规则/契约/坑/工作量 + 业务实体 + 来源路径）归并进**本工程** `docs/数字员工/` 下的「{工程名}-工程约定」文档对应分节；开工先检索本工程 `docs/数字员工/知识卡/`、热记忆与约定文档 → 再读知识卡原文（Gate C 落盘前用户过目确认）
- **模板来源（通用机制）**：通用模板在预设 `skills/digital-employee/resources/templates/需求知识卡-模板.md`；本工程 `docs/数字员工/` 是其副本，**换工程时由预设自动初始化新工程的沉淀目录**，每个工程各有一套、互不混用
- 备用资产：`docs/数字员工/流程蓝图/`（5 条流程蓝图，**当前默认不启用**）、`docs/数字员工/签核台账-模板.md`（可选，不强制）
