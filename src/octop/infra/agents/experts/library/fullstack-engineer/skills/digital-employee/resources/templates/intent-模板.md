---
# 交付物元数据（轻量结构化）：全字段必填；需求ID 与签核台账、各交付物共用。规则见数字员工 AGENTS.md / SKILL.md §2.1。
需求ID: "REQ-{YYYYMMDD}-{NN}"
需求名称: "{任务名}"
文档类型: "intent（节点1 理解）"
分类: "【需求】/【问题】/【优化】"
版本: "v1.0"
评审人: ""
返工次数: 0
上线状态: "未上线"
关联签核台账: "docs/数字员工/签核台账/{需求ID}-签核台账.md"
---

# Intent：{任务名}

> 对齐 Anthropic《The AI-Native SDLC playbook》的 `intent.md`（Stage 1·Plan）。节点1「理解」确认后落盘并提交（作者/时间戳进 git 历史，成为提交物链审计线索第一环），然后才建 worktree。
> **用提出者的原话写，无正式措辞要求；内容由头脑风暴时主动澄清的范围/用户/约束/成功标准支撑。**

- 工程：{工程名} ｜ 提出者：{提出人} ｜ 状态：draft → 用户确认后 accepted ｜ 需求ID：{需求ID}

## Problem
{用业务语言 1~3 句：当前做不到什么、谁受影响；或贴用户原话}

## Proposed outcome
{希望达到的结果：用户看到/能做/接口返回什么}

## Affected users and systems
{受影响用户 / 前端 / 后端模块 / 依赖系统}

## Constraints
{硬约束：合规、资金安全、性能、兼容、不引入的改动等}

## Open questions
{待用户/工程拍板的问题——必须在进入 spec 前澄清或标记延续}

## 出口 Gate（节点1）
- [ ] 已检索本工程沉淀（`docs/数字员工/知识卡`、热记忆、`{工程名}-工程约定`）
- [ ] 用户已确认理解，本 `intent.md` 落盘提交，需求ID 已分配
- [ ] worktree 已创建（`git worktree add -b task/<topic>-YYYYMMDD-HHMMSS`）
