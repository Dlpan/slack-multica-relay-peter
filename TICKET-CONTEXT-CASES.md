# 工单语义验收

自动测试只验证可确定的身份、字段、去重和回读边界；以下语义场景在部署后用 owner 主动指定的测试 ticket 验收，不擅自重放历史消息。

| 场景 | 预期 |
|---|---|
| Peter 直接 @自己要求回复；Slack 已有明确根因 | ack；英文 #intercom；回读后完成表情 |
| 原因只有本地项目 Codex 聊天有记录，标题无 ticket key | 按正文准确 key 检索，读取后续纠正和执行证据；不只看标题 |
| 本地 Codex 不可用，Slack 证据足够 | 记录限制后继续回复 |
| 两个来源有未解决的根因冲突 | 不发客户评论；移除 ack；中文说明冲突 |
| Peter 说 close；只有代码修改但未部署 | 不检查生效或合并；补齐实际必填字段并关闭 |
| Peter 要求关闭，但没有根因且工作流不要求根因 | 可以关闭，不额外要求根因 |
| 关闭必填 Cause and Solution 缺少事实 | 不编造；中文说明需要补充内容 |
| 非 CS 项目明确 reply/close | 同样处理，读取该项目实时工作流 |
| 他人 @Peter 要求关单 | 不执行工单动作、不加工单 reaction、不回复；仍可独立处理真正 PR Review |
| owner 仅 @grm-fe，或 Agent 代 owner 发消息 | 不执行工单动作 |
| “先别关”“能关吗？”或引用别人的关单请求 | 不按关闭指令执行 |
| thread 内多个 ticket，当前消息明确一个 | 只处理该 ticket；不明确则中文澄清 |
| 只要求回复，但已解决 | 不自动关闭 |
| 只要求关闭 | 不新增 #intercom 评论 |
| 同时回复和关闭，回复成功但缺关单必填字段 | 保留评论、不重复发送；无完成表情；中文说明已回复但未关闭 |
| 同消息重投或多个 thread 同时操作同 ticket | 读取持久状态/同 ticket 锁；不盲目重发 |
| POST 超时但已写入 | 回读恢复；未知时停止，不重复 POST |
| 调查过程中用户编辑原指令或撤销 | 原文变化阻断；Agent 读取新回复发现撤销则停止 |
| 既有 PR Review 首次/复审/用户组/允许的应用 | 沿用原权限和 reaction，不受工单本人限制影响 |
| Bug Online 在 In Progress，只有 Bug Fixed | 补齐三个英文必填字段，进入 In Testing 后重新查可用动作，再到 Resolved；最后加完成表情 |
| Open 只有 Start Dev | 原因/解决信息齐全后先进入 In Progress，再按 Bug Fixed → In Testing → Resolved；信息不足不先改状态 |
| Bug Fixed 三字段的 required=false | 仍检查 Dev Result、Root Cause 和 Solution，不把 API 非必填当作可以省略 |
| 已在 In Testing | 只执行当前可用的 Resolved 动作，不退回 Open 或重复 Bug Fixed |
| 已到 In Testing，但后续动作/字段/权限不满足 | 中文说明最后确认状态，不标记完成；重试从中间状态继续 |
| 中间或最终 POST 超时 | 回读确认到达目标且字段一致才继续；未知结果不重发、不倒退状态 |
| Bug 最后一步先返回 400，补齐字段后重试成功 | 中途保留 ack，不发失败文字；只执行剩余流转，最后添加完成表情 |
| Bug 已到 In Testing，但缺少后续必要事实 | Agent 确认无法继续后 finalize-failure；说明已流转至 In Testing，不写“已完成：无” |
| 写入响应超时但 Jira 实际已 Resolved | finalize-failure 拒绝旧失败通知；先运行 helper 对账 |
| 同一触发消息已发最终失败，后续成功恢复 | 更正已记录的失败消息，不新增矛盾的失败文字 |
