# 启动引导

此文件提供在 Agent 启动时加载的额外上下文。

## 项目背景

此 Agent 是 tinyClaw 教学框架的一部分，演示如何从零构建 AI Agent Gateway。
workspace 目录包含塑造 Agent 行为的配置文件：

- SOUL.md: 个性和沟通风格
- IDENTITY.md: 角色定义和边界
- TOOLS.md: 可用工具和使用指南
- MEMORY.md: 长期事实和偏好
- HEARTBEAT.md: 主动行为指令
- BOOTSTRAP.md: 本文件 -- 额外的启动上下文
- AGENTS.md: 多 Agent 协作说明
- CRON.json: 定时任务定义

## 工作区布局

```
workspace/
  *.md          -- Bootstrap 文件（加载到系统提示词）
  CRON.json     -- Cron 任务定义
  memory/       -- 每日记忆日志
  skills/       -- 技能定义
  .sessions/    -- 会话记录（自动管理）
  .agents/      -- 每个 Agent 的状态（自动管理）
```
