# Slack → Multica Relay

在获准频道中 @真人或 User Group，把请求交给 Multica 的专用 Agent，执行环境可以是本地 Codex。适配 Vercel Functions 与 EdgeOne Cloud Functions，共用同一套处理逻辑。

## 链路

`Slack → 签名与准入校验 → QStash 持久化 → HTTP 200 → 消费函数 → Multica Issue → Agent/Runtime → Slack 回复`

- 只处理当前消息明确 mention 的事件。Team 必填；频道和发送者支持白名单（可设为 `all`）及黑名单，黑名单优先。未许可的应用、Bot、编辑/删除和未命中目标 mention 的事件不触发。
- 入站只等待 QStash 接收；队列负责后台投递及3次重试，耗尽后在其失败队列查看/重放。
- 每个 Slack thread 通过普通 Issue API 创建独立任务卡，不经过 Autopilot 同标题60秒去重。
- thread scope 包含 Workspace、Project、Agent；不同 Agent 配置不采用彼此的映射。
- 同 thread 后续消息追加评论。QStash 按 thread 限并发，Redis 锁与消息状态处理重投。
- 标题使用消息摘要与稳定的线程短标识；描述和后续评论分为原文引用、来源及 JSON 上下文，附件仅保留定位字段。
- 描述首行的 `relay-thread` 标记和 `relay-payload:v1` 数据区块用于 KV 映射过期后的恢复，请勿删除或修改。读取兼容历史裸 JSON；损坏的数据会停止恢复，不自动重新建卡。
- 写请求结果不明时先查回读；查不到则保留 ambiguous 错误，不盲目再次 POST。需要人工核对/重放，不承诺 exactly-once。
- `comment_persisted` 只表示评论保存，实际执行和原 thread 回复要分别验收。
- Prompt 真源为 [AGENT-PROMPT.md](AGENT-PROMPT.md)，需要明确同步到 Multica Agent instructions。Relay 不调用 Codex 或修改 Multica 源码。

## 仅执行 PR Review：统一由 Agent 判断 thread

设置 `SLACK_TASK_FILTER=pr_review`。Relay 只检查签名、来源、当前消息的目标 mention 和频道/发送者策略，不再根据正文关键词、固定短语或 PR 链接决定是否入队。主消息和 thread 回复走同一路径；`cc`、修改说明、交回结果等只是上下文资料，不是触发白名单。

消费者根据当前服务器配置生成独立的 `taskPolicy`（`source=relay_config`、`mode=pr_review`、`reactionName`），写到任务描述或后续评论中；Slack 事件不能注入或覆盖该策略。此模式仅允许 Agent 做 PR Review。**收件或建卡不等于确认评审请求**：非 PR mention 也可能产生一张用于语义判断的任务卡，占用一次 Agent 执行，但不能因此执行非评审任务。

Agent 必须以 User 身份完整读取同一 thread 的根消息、触发之前的历史回复（含分页）、相关 PR 和必要资料，结合既有评审、修改与交回过程及当前意图判断是否需要评审。PR 链接配评审组 mention 和修改说明可以表达首次评审，已有 Review 中交回修改可以表达复审；不要求当前消息重复 PR 链接或出现命令句。thread 有 PR 不代表每个 mention 都要求评审，非评审、仅同步、明确撤销或征求真人拍板时只在 Multica 记录判断，Slack 保持静默。必要的 thread 文本或 PR 代码不可读取、或多个 PR 目标不清时记录阻断，不猜测。截图等附件为补充证据：缺少文件权限、下载失败或格式不支持时继续审查可读的 PR 描述、diff、评论和 CI，仅说明依赖附件的未验证项，不阻塞整项任务，也不冒充已验证。

`pr_review` 模式下 Relay 不再在建卡时添加 reaction。Agent 确认需要评审并校验 User 身份后，才在触发消息添加配置的 reaction（先检查避免重复）；表示确认接单，不表示评审完成。旧 payload/旧版 Relay 已添加的 reaction 无需重复。

默认 `all` 保留原有通用模式和 Relay reaction；未知配置值拒绝处理。身份、应用来源、频道和发送者策略在入队与消费时均校验。上线前必须同步 [Agent Prompt](AGENT-PROMPT.md)；Peter 等定制 Agent 应合入上述语义判断与 reaction 规则，保留各自的身份和写回约束。回滚时，Prompt 与 Relay 的判断及 reaction 行为必须一起核对。

## 用户组与 User 应用消息

用户组 mention 需要在 `SLACK_TARGET_SUBTEAM_IDS` 配置准确的组 ID；属于该组不会自动使个人 mention 规则命中。组与个人目标同时配置时任意一个命中即可，频道/发送者规则继续适用，PR 意图由 Agent 判断。

Slack 应用通过 User 身份发送的消息也可能带 `bot_id` 和 `app_id`。默认忽略应用消息；如确需接收已核实的来源，在 `SLACK_ALLOWED_APP_ACTORS` 中配置准确的 `app_id:user_id` 配对，多个配对用逗号分隔。必须先核实发送人为真人账号，只开放所需应用与发送人，不支持通配符，也不要加入 Relay 自己的回复身份。`bot_message`、编辑和删除事件仍被忽略。

应用来源的 `sourceAppId` 会保留到队列，在消费时重新核对配对，撤销配置后旧队列消息也不能继续执行。启用用户组时还应同步 Agent prompt 的目标范围，不能继续要求每条消息都直接 mention 个人。环境变量变更后需要重新部署。

## 本地验证

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm lint
```

配置与两平台部署见 [搭建手册](SETUP-GUIDE.zh-CN.md)，契约边界见 [审查记录](REVIEW.md)。

## 状态与日志

日志记录关联标识、耗时和有限错误码。正文保存在队列和 Multica；Redis 保存线程/消息状态，不存 pending 正文。状态保留90天。内容级调试日志尚未启用，凭据不进入日志。

| 接口结果                | 含义                                                                   |
| ----------------------- | ---------------------------------------------------------------------- |
| 入站 accepted / HTTP200 | QStash 已接收，不代表 Agent 完成                                       |
| 入站 ignored / HTTP200  | 不满足触发范围                                                         |
| 入站503                 | 收件未确认，交给 Slack 重试                                            |
| 消费 created            | Issue 已创建或从回读恢复                                               |
| 消费 comment_persisted  | 后续评论已保存                                                         |
| 消费 duplicate          | 已处理的消息                                                           |
| 消费503                 | 保留队列重试/DLQ责任，原因包括 timeout、thread*lock_busy、ambiguous*\* |

`GET /api/health` 仅证明函数可响应。消费有45秒整体预算，部署函数上限60秒；入站发布请求超时2秒。平台冷启动、网络延迟与配额仍须实测。

语义验收场景见 [PR Review 上下文用例](REVIEW-CONTEXT-CASES.md)。单元测试验证传输、配置和身份边界，不将关键词匹配结果冒充 Agent 的语义判断结果。
