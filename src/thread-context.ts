import type { RelayConfig } from "./config.js";
import { isSupportedMessage, type SlackMessageEvent } from "./mentions.js";
import { hasThreadReviewContext } from "./task-filter.js";
import type { SlackThreadEvent } from "./thread-router.js";

/** Resolve context in the consumer so Slack's acknowledgement never waits on history. */
export async function readThreadReviewContext(
  event: SlackThreadEvent,
  config: RelayConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<boolean> {
  if (event.threadTs === event.messageTs) return false;
  let cursor = "";
  const seen = new Set<string>();
  for (let page = 0; page < 3; page++) {
    const url = new URL("https://slack.com/api/conversations.replies");
    url.search = new URLSearchParams({ channel: event.channelId, ts: event.threadTs,
      latest: event.messageTs, inclusive: "false", limit: "100", ...(cursor ? { cursor } : {}) }).toString();
    const response = await fetchImpl(url.toString(), {
      headers: { authorization: `Bearer ${config.slackReactionToken}` },
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) throw new Error("slack_thread_unavailable");
    const body = await response.json() as {
      ok?: boolean; messages?: SlackMessageEvent[]; has_more?: boolean;
      response_metadata?: { next_cursor?: string };
    };
    if (body?.ok !== true || !Array.isArray(body.messages)) throw new Error("slack_thread_unavailable");
    const texts = body.messages.filter(message => message
      && isSupportedMessage({ ...message, channel: event.channelId }, config.allowedAppActors)
      && typeof message.ts === "string" && /^\d+\.\d+$/u.test(message.ts)
      && Number(message.ts) < Number(event.messageTs)
      && (message.ts === event.threadTs || message.thread_ts === event.threadTs)
      && typeof message.user === "string"
      && !config.blockedSenderIds.has(message.user)
      && (config.allowAllSenders || config.allowedSenderIds.has(message.user)))
      .map(message => message.text as string);
    if (hasThreadReviewContext(texts)) return true;
    const next = body.response_metadata?.next_cursor;
    if (next !== undefined && typeof next !== "string") throw new Error("slack_thread_unavailable");
    if (!next && !body.has_more) return false;
    if (!next || seen.has(next)) throw new Error("slack_thread_unavailable");
    seen.add(next);
    cursor = next;
  }
  // Keep exhausted history reads visible/retryable instead of silently dropping a request.
  throw new Error("slack_thread_limit");
}
