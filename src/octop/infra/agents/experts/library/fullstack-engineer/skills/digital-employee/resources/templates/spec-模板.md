---
# 交付物元数据（轻量结构化）：全字段必填。需求ID 与签核台账、各交付物共用。
需求ID: "REQ-{YYYYMMDD}-{NN}"
需求名称: "{任务名}"
文档类型: "spec（节点2 计划与设计 · 需求与设计）"
分类: "【需求】/【问题】/【优化】"
版本: "v1.0"
评审人: ""
返工次数: 0
上线状态: "未上线"
关联签核台账: "docs/数字员工/签核台账/{需求ID}-签核台账.md"
---

# Spec：{任务名}（需求与设计）

> 对齐 Anthropic playbook 的 `spec.md`（Stage 2·Design）：由已接受的 `intent.md` 产出，受工程规范/技能约束（安全/合规/UX/兼容）；**逐处标出 flagged concerns**。对应签核制「详细设计报告」。节点2 与 `plan.md` 一起，Gate A 等用户点头后才编码。

## 需求说明（来自 intent.md）
- Problem 对应解决方式：
- Open questions 的处理（已回答 / 延续到实现）：

## 设计
- 后端：数据模型 / 业务逻辑 / API（接口、入参、出参、异常、幂等约定）
- 前端：页面 / 组件 / 交互 / 响应式 / 无障碍
- 集成 / 依赖 / 兼容性：
- 不做的（Out of scope，延续 intent）：

## 政策与约束落地（含 flagged concerns）
- 安全 / 合规 / 品牌 / UX 逐项标注如何满足；
- **Flagged concerns**：无法两全或需要政策责任人拍板的点（如与既有实现冲突、存在互斥政策）。

## Spec 自检（防理解偏差）
- [ ] 本 spec 是否解决了 intent 声明的 Problem？
- [ ] intent 的 Open questions 是否已回答或延续？
- [ ] Flagged concerns 是否已逐条标注处理或上报人？

## 出口 Gate A（节点2）
- [ ] 开发计划/测试用例设计参考 `plan-模板.md`（同目录），本 spec 已落盘
- [ ] **等用户正式点头（Gate A）后才开始编码**
