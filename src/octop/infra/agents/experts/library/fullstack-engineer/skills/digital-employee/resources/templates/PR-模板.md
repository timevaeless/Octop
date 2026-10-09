# PR 模板

> 通用模板。按 Git 工作流决策（第 6 节）使用；工程无 PR 约定时采用。

## Feature

**Title**: `feat: <concise description>`（单行 ≤120 字符，禁 `\n`/`\r`）

```markdown
## What changed
- 需求ID: {需求ID}
- [按文件/模块描述]

## Verification
- Tests: [pass/fail summary]
- Build: [pass/fail]
- [other verification results]
```

## Bug Fix

**Title**: `fix: <concise description>`

```markdown
## Bug
- 需求ID: {需求ID}
- Symptom: [observed]
- Root cause: [what actually went wrong]

## Fix
- [describe the fix]

## Verification
- Regression tests: [evidence]
- Validation ladder: [stopping point + residual risk]
```

## 元数据安全（硬规则）

- 从结构化载荷构造（JSON），不做裸 shell 字符串拼接。
- body 用真实多行文本（LF），不把换行编码成字面 `\n`；禁控制字符（0x00-0x08、0x0B、0x0C、0x0E-0x1F、0x7F）。
- 发布前检查：标题/正文无转义换行字面量，段落用真实换行；不满足则停止提交并报告违规字段。
