import { describe, expect, it, vi } from "vitest";
import { loadRelayConfig } from "../src/config.js";
import { readThreadReviewContext } from "../src/thread-context.js";
import type { SlackThreadEvent } from "../src/thread-router.js";

const config = loadRelayConfig({
  SLACK_SIGNING_SECRET: "test", SLACK_TEAM_ID: "T1", SLACK_TARGET_USER_IDS: "U1",
  MULTICA_API_BASE_URL: "https://multica.test", MULTICA_API_TOKEN: "test",
  MULTICA_WORKSPACE_ID: "ws", MULTICA_PROJECT_ID: "project", MULTICA_AGENT_ID: "agent",
  SLACK_REACTION_TOKEN: "user-test", SLACK_REACTION_NAME: "eyes",
  KV_REST_API_URL: "https://kv.test", KV_REST_API_TOKEN: "test", QSTASH_TOKEN: "test",
  QSTASH_CURRENT_SIGNING_KEY: "test", QSTASH_NEXT_SIGNING_KEY: "test",
  RELAY_CONSUMER_URL: "https://relay.test/api/queue/consume", SLACK_ALLOWED_APP_ACTORS: "A2:U2",
});
const event: SlackThreadEvent = { teamId: "T1", channelId: "C1", senderUserId: "U2",
  messageTs: "100.000001", threadTs: "99.000001", text: "<@U1> 再 cc", mention: { type: "user", id: "U1" } };
const parent = { type: "message", ts: event.threadTs, user: "U2", text: "帮忙看看 PR：<https://github.com/org/repo/pull/123>" };

describe("thread context lookup", () => {
  it("reads only this thread with the configured User token and accepts an approved User app", async () => {
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ok: true, messages: [{ ...parent, app_id: "A2", bot_id: "B2" }] }));
    expect(await readThreadReviewContext(event, config, f)).toBe(true);
    const [input, init] = f.mock.calls[0]!;
    const url = new URL(String(input));
    expect(url.origin + url.pathname).toBe("https://slack.com/api/conversations.replies");
    expect(Object.fromEntries(url.searchParams)).toMatchObject({ channel: "C1", ts: event.threadTs, latest: event.messageTs, inclusive: "false" });
    expect(init?.headers).toEqual({ authorization: "Bearer user-test" });
  });
  it.each([
    { ts: event.messageTs, thread_ts: event.threadTs },
    { ts: "101.000001", thread_ts: event.threadTs },
    { ts: "99.000002", thread_ts: "98.000001" },
    { bot_id: "B2" }, { app_id: "A3", bot_id: "B2" },
    { subtype: "bot_message" }, { subtype: "message_deleted" },
    { text: "不用 review PR https://github.com/org/repo/pull/123" },
  ])("ignores ineligible historical messages: %j", async change => {
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ok: true, messages: [{ ...parent, ...change }] }));
    expect(await readThreadReviewContext(event, config, f)).toBe(false);
  });
  it("does not use a blocked sender to supply review context", async () => {
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ok: true, messages: [parent] }));
    expect(await readThreadReviewContext(event, { ...config, blockedSenderIds: new Set(["U2"]) }, f)).toBe(false);
  });
  it("follows pagination to a prior review request", async () => {
    const f = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json({ ok: true, messages: [], has_more: true, response_metadata: { next_cursor: "page2" } }))
      .mockResolvedValueOnce(Response.json({ ok: true, messages: [{ ...parent, ts: "99.000002", thread_ts: event.threadTs }] }));
    expect(await readThreadReviewContext(event, config, f)).toBe(true);
    expect(new URL(String(f.mock.calls[1]![0])).searchParams.get("cursor")).toBe("page2");
  });
  it("fails visibly if Slack truncates history without a usable cursor", async () => {
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ok: true, messages: [], has_more: true }));
    await expect(readThreadReviewContext(event, config, f)).rejects.toThrow("slack_thread_unavailable");
  });
  it("bounds history reads and leaves excessive history retryable", async () => {
    const f = vi.fn<typeof fetch>().mockImplementation(async () => Response.json({ ok: true, messages: [], response_metadata: { next_cursor: String(f.mock.calls.length) } }));
    await expect(readThreadReviewContext(event, config, f)).rejects.toThrow("slack_thread_limit");
    expect(f).toHaveBeenCalledTimes(3);
  });
});
