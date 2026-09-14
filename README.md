# Slack → Multica Relay

在获准频道中 @真人或 User Group，把请求交给 Multica 的专用 Agent，执行环境可以是本地 Codex。适配 Vercel Functions 与 EdgeOne Cloud Functions，共用同一套处理逻辑。

## 链路

`Slack → 签名与准入校验 → QStash 持久化 → HTTP 200 → 消费函数 → Multica Issue → Agent/Runtime → Slack 回复`

- 只处理当前消息明确 mention 的事件。Team 必填；频道和发送者支持白名单（可设为 `all`）及黑名单，黑名单优先。Bot、编辑/删除、普通讨论不触发。
- 入站只等待 QStash 接收；队列负责后台投递及3次重试，耗尽后在其失败队列查看/重放。
- 每个 Slack thread 通过普通 Issue API 创建独立任务卡，不经过 Autopilot 同标题60秒去重。
- thread scope 包含 Workspace、Project、Agent；不同 Agent 配置不采用彼此的映射。
- 同 thread 后续消息追加评论。QStash 按 thread 限并发，Redis 锁与消息状态处理重投。
- 标题使用消息摘要与稳定的线程短标识；描述和后续评论分为原文引用、来源及 JSON 上下文，附件仅保留定位字段。
- 描述首行的 `relay-thread` 标记和 `relay-payload:v1` 数据区块用于 KV 映射过期后的恢复，请勿删除或修改。读取兼容历史裸 JSON；损坏的数据会停止恢复，不自动重新建卡。
- 写请求结果不明时先查回读；查不到则保留 ambiguous 错误，不盲目再次 POST。需要人工核对/重放，不承诺 exactly-once。
- `comment_persisted` 只表示评论保存，实际执行和原 thread 回复要分别验收。
- Prompt 真源为 [AGENT-PROMPT.md](AGENT-PROMPT.md)，需要明确同步到 Multica Agent instructions。Relay 不调用 Codex 或修改 Multica 源码。

## 仅接收 PR Review

设置 `SLACK_TASK_FILTER=pr_review`，同时保留目标用户 ID 校验。若需让所有可接收事件的频道、所有发送者都能发起请求，可将 `SLACK_ALLOWED_CHANNEL_IDS` 和 `SLACK_ALLOWED_SENDER_IDS` 设为 `all`。此设置不会扩大 Slack App 自身的访问权限。

触发消息本身必须同时包含 PR 线索（`PR`、`pull request`、合并请求或 GitHub PR 链接）和评审意图（`review`、`CR`、审查、评审、审核、看下等）。例如 `@Peter 请 review https://github.com/org/repo/pull/123`；普通提及、只贴链接、仅要求合并、明确不用评审或已完成评审的消息会被忽略。代码片段、引用消息及链接标签不参与意图判断；不会读取父消息补全 PR 线索，复审时请写明 `重新 review PR #123`。

过滤在入队前和消费时均执行，策略变更后旧队列消息也须满足当前规则。默认 `all` 保持原行为；未知配置值会拒绝处理。这里是保守的文本规则，并非完整语义识别，Agent 仍须核对实际请求，只执行 PR review，非评审请求保持静默。启用时先部署过滤代码与 Agent 规则，再开放频道和发送者范围；回滚到旧代码前先恢复受限范围。

## 用户组与 User 应用消息

用户组 mention 需要在 `SLACK_TARGET_SUBTEAM_IDS` 配置准确的组 ID；属于该组不会自动使个人 mention 规则命中。组与个人目标同时配置时任意一个命中即可，PR 内容与频道/发送者规则继续适用。

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
