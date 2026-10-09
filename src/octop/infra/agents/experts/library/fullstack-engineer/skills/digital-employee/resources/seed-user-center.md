# 种子工程约定：奇瑞用户中心（user-center）

> 本文件为**种子工程约定**（对齐 `resources/seed-summer-cloud.md` 的用途），由技能 digital-employee 携带；**换工程时按学习协议为该工程实际裁剪**，首次进入真实任务后再用证据校准、不当作绝对事实。审计时点：2026-09-19。

## 工程组成
- 后端：`user-center`（`cn.cheryfs`，Maven 多模块 `cfp-parent`，当前 `1.6.11.19-SNAPSHOT`）
- 前端：`user-center-front`（包名 `qirui`，RuoYi 3.8.5 管理后台模板）

## 后端约定
- 模块：`cfp-auth`（认证）/ `cfp-user`（用户）/ `cfp-common`（通用），父 POM 聚合。
- 骨架：RuoYi / 若依风格（Controller-Service-Mapper），登录走 session/token（RuoYi 式），**审计时点未引入 JWT**。
- 部署环境目录：`pre/`、`sit/`、`uat/`、`prod/`（按环境分层）；`docs/`、`logs/`、`templates/` 在仓库内。

## 前端约定
- Vue 2.6 + Element UI，`axios 0.24.0`；用 `vue-cli-service`。
- npm 脚本：`dev` / `build:prod`（生产）/ `build:stage` / `preview` / `lint`（`eslint --ext .js,.vue src`）。
- husky pre-commit 配 `lint-staged`（提交前 `eslint --fix`）。

## 现状（审计时点，防假设用证据）
- 注册：前端已有 `src/views/register.vue`（**用户名+密码**）与 `src/api/login.js`，调用 `/register`。
- 后端：**未搜到 `/register` 的 Controller 映射，未搜到任何 JWT 机制**（grep 无命中）。
- 两个仓库均**无 `docs/数字员工/`** 沉淀（工程知识库未初始化）。

## 分支约定
- 功能分支 `feature-*`、发布分支（如 `prod-1.1`）并存，多分支并行；
  **多分支并行 ⇒ 新需求/缺陷/优化必须 worktree**（技能硬规则），主检出保持主干不动。
- 审计时点当前分支：后端 `feature-双重登录验证`、前端 `prod-1.1`。

## 待真实任务确认（不得臆断）
- 密码字段/加密方式、是否引入 JWT 及放置位置、部署与发版清单规范、单测/覆盖率基线——按真实任务与工程实际确认后再固化为长期约定。
