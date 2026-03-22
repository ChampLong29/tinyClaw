# 第 1 章实施包: Agent 循环精讲

适用目标: 吃透最小 Agent 闭环, 为后续 9 章建立统一心智模型。

对应源码:
- sessions/zh/s01_agent_loop.py
- sessions/zh/s01_agent_loop.md

---

## 1. 章节口述稿 (5-8 分钟)

这一章只有一个核心结论: Agent 的最小本质就是一个循环。

循环每轮只做三件事:
1) 收输入: 从终端读取用户文本。
2) 调模型: 把当前 messages 发送给 LLM。
3) 做分支: 看 stop_reason 决定下一步。

为什么这是最小闭环?
因为只要这三步成立, 你就已经有了可持续对话能力。后面的工具、会话、路由、可靠性, 都只是往这个闭环上叠加能力, 不是推翻这个闭环。

关键状态只有一个: messages。
- 每来一条用户输入, append 一条 user 消息。
- 每收到模型结果, append 一条 assistant 消息。
- 下一轮调用时, 模型读取的是完整历史, 所以上下文能连续。

关键决策只有一个: stop_reason。
- end_turn: 本轮结束, 打印输出, 继续下一轮。
- tool_use: 模型想调用工具。第 1 章不执行工具, 但代码预留了分支。第 2 章直接接上。
- 其他情况: 做兜底输出, 仍然把 assistant 内容写入 messages。

注意一个非常实战的点: API 报错时把刚 append 的 user 消息弹掉。
这样用户可以直接重试, 不会把失败请求污染历史。

这一章结束后你要形成一个固定句式:
Agent = while + messages + stop_reason。

---

## 2. 关键图谱 (流程/调用链)

### 2.1 主流程图

User Input
  -> messages.append(user)
  -> client.messages.create(model, system, messages)
  -> if stop_reason == end_turn:
       提取文本并打印
       messages.append(assistant)
     elif stop_reason == tool_use:
       记录提示(本章无工具)
       messages.append(assistant)
     else:
       兜底处理并 messages.append(assistant)
  -> 下一轮 while

### 2.2 状态变化图

初始:
messages = []

第 1 轮后:
messages = [
  {role:user, content:Q1},
  {role:assistant, content:A1}
]

第 2 轮后:
messages = [
  {role:user, content:Q1},
  {role:assistant, content:A1},
  {role:user, content:Q2},
  {role:assistant, content:A2}
]

### 2.3 函数调用链

main
  -> 环境变量检查
  -> agent_loop
     -> input
     -> messages.append(user)
     -> client.messages.create
     -> stop_reason 分支
     -> messages.append(assistant)

---

## 3. 高频追问与标准回答

Q1: 为什么 messages 要保存完整历史? 只传最后一条不行吗?
A1: 只传最后一条会丢失上下文。多轮对话依赖历史语义链, 例如代词指代、前文约束和风格延续。完整历史是最直接、最稳定的上下文机制。

Q2: 既然本章没工具, 为什么还要写 tool_use 分支?
A2: 这是架构前向兼容。外层循环不再改动, 第 2 章只在分支内接入工具执行即可。这样系统演进是加法, 不是重构。

Q3: API 异常时为什么要 pop 最后一条 user 消息?
A3: 因为该轮请求没有成功进入模型推理, 保留这条 user 消息会导致历史与真实响应不一致, 重试时会重复语义并污染对话状态。

Q4: stop_reason 只有 end_turn 和 tool_use 吗?
A4: 实际还可能有 max_tokens 等。代码里有 else 兜底, 先确保可观察和可继续, 避免循环被未知分支打断。

Q5: 这一章和生产系统的关系是什么?
A5: 生产系统把这个循环嵌入更复杂组件, 但主循环本质不变。后续章节做的是持久化、路由、调度、可靠性和并发控制。

Q6: 为什么 assistant 追加的是 response.content 而不是纯文本?
A6: 保留结构化块更通用。后续工具调用会出现非纯文本块, 统一保存原始块结构能避免信息损失。

---

## 4. 逐行走读锚点 (建议按此顺序讲)

1) 环境与客户端初始化
- 加载 .env 与 MODEL_ID
- 初始化 Anthropic client
- 设定 SYSTEM_PROMPT

2) 交互入口
- main 先校验 ANTHROPIC_API_KEY
- 再进入 agent_loop

3) 循环主体
- 读输入 + 退出条件
- append user
- 调 API
- stop_reason 三分支
- append assistant

4) 错误处理
- API 异常捕获
- 回滚 user 消息
- continue 重试

---

## 5. 本章验收清单

- 能解释为什么说 Agent 的最小本质是 while + stop_reason。
- 能画出 messages 在两轮对话后的状态。
- 能解释 end_turn 与 tool_use 的分工。
- 能说明 API 失败时 pop 的设计目的。
- 能口头说明本章与第 2 章的衔接点。

---

## 6. 上机操作与观察点

运行:
python sessions/zh/s01_agent_loop.py

观察点:
1) 连续问两轮相关问题, 验证模型记忆前文。
2) 输入空行, 验证循环直接 continue。
3) 输入 quit 或 exit, 验证优雅退出。
4) 临时填错 key, 验证 main 的环境校验与错误提示。

---

## 7. 面试表达模板 (60 秒)

我会先从最小 Agent 闭环讲起。第 1 章实现了一个 while 循环, 每轮把用户输入追加到 messages, 调用 LLM, 再根据 stop_reason 做分支。end_turn 直接输出并写回 assistant 消息, tool_use 分支先预留为后续扩展。这个设计的关键是把对话状态收敛到 messages 一个对象, 并在 API 异常时回滚 user 追加, 保证状态一致性。后续章节都是在这个闭环上做增量增强, 比如工具、持久化、路由、可靠性和并发。
