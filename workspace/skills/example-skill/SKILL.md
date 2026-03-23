---
name: 示例技能
description: 一个示例技能，用于演示技能系统的工作原理
invocation: /示例
---
# 示例技能

当用户调用 /示例 时，友好地问候并解释这是从 workspace skills 目录加载的示例技能。

你可以通过在 `workspace/skills/` 下添加新目录来创建自己的技能，每个目录需要包含一个 `SKILL.md` 文件，格式如下：

```markdown
---
name: 技能名称
description: 技能描述
invocation: /触发指令
---
# 技能说明

在这里描述技能的具体功能和使用方法。
```

技能的 frontmatter 字段说明：
- `name`: 技能的内部名称
- `description`: 技能的简短描述，会显示给用户
- `invocation`: 用户触发技能的命令（通常以 / 开头）
