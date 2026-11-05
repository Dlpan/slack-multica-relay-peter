import { createHmac } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { acceptSlack, consumeQueue, isReleaseApprovalCandidate } from "../src/http.js";
import { loadRelayConfig } from "../src/config.js";

vi.mock("@upstash/qstash", () => ({ Receiver: class { verify = vi.fn().mockResolvedValue(true); } }));

const env = {
  SLACK_SIGNING_SECRET: "test", SLACK_TEAM_ID: "T1", SLACK_TARGET_USER_IDS: "U03JFM4M82C",
  SLACK_ALLOWED_CHANNEL_IDS: "CMAIN", MULTICA_API_BASE_URL: "https://multica.test",
  MULTICA_API_TOKEN: "test", MULTICA_WORKSPACE_ID: "ws", MULTICA_PROJECT_ID: "review-project",
  MULTICA_AGENT_ID: "review-agent", SLACK_REACTION_TOKEN: "test", SLACK_REACTION_NAME: "eyes",
  KV_REST_API_URL: "https://kv.test", KV_REST_API_TOKEN: "test", QSTASH_TOKEN: "test",
  QSTASH_CURRENT_SIGNING_KEY: "test", QSTASH_NEXT_SIGNING_KEY: "test",
  RELAY_CONSUMER_URL: "https://relay.test/api/queue/consume", SLACK_TASK_FILTER: "pr_review",
  RELEASE_APPROVAL_ENABLED: "true", RELEASE_APPROVAL_OWNER_USER_ID: "U03JFM4M82C",
  RELEASE_APPROVAL_REQUESTER_USER_IDS: "U06GM8PAFEX,U03JFM4M82C,U06RHAA209Y",
  RELEASE_APPROVAL_CHANNEL_IDS: "C01K60LEP7E", RELEASE_APPROVAL_ROOT_BOT_USER_IDS: "U081137G64T",
  RELEASE_APPROVAL_ROOT_BOT_IDS: "B081PVALKC0",
  RELEASE_APPROVAL_REPOSITORIES: "MoeGolibrary/moego-online-booking-client-web,MoeGolibrary/Boarding_Desktop,MoeGolibrary/moego-mobile",
  RELEASE_APPROVAL_ENVIRONMENT: "deploy-approval", RELEASE_APPROVAL_EXECUTE: "false",
  MULTICA_RELEASE_APPROVAL_PROJECT_ID: "release-project", MULTICA_RELEASE_APPROVAL_AGENT_ID: "release-agent",
};

function signed(event: unknown): Request {
  const body = JSON.stringify({ type: "event_callback", team_id: "T1", event });
  const ts = String(Math.floor(Date.now() / 1000));
  const signature = "v0=" + createHmac("sha256", "test").update(`v0:${ts}:${body}`).digest("hex");
  return new Request("https://relay.test/api/slack/events", { method: "POST", body,
    headers: { "x-slack-request-timestamp": ts, "x-slack-signature": signature } });
}

describe("release approval routing", () => {
  it("fails closed when enabled policy is incomplete or broadened", () => {
    expect(() => loadRelayConfig({ ...env, RELEASE_APPROVAL_ENVIRONMENT: "production" })).toThrow("invalid_release_approval_environment");
    expect(() => loadRelayConfig({ ...env, RELEASE_APPROVAL_REQUESTER_USER_IDS: "UWINCHES,ULUZ" })).toThrow("invalid_release_approval_owner");
    expect(() => loadRelayConfig({ ...env, RELEASE_APPROVAL_REQUESTER_USER_IDS: `${env.RELEASE_APPROVAL_REQUESTER_USER_IDS},UOTHER` })).toThrow("invalid_release_approval_owner");
    expect(() => loadRelayConfig({ ...env, RELEASE_APPROVAL_CHANNEL_IDS: "CRELEASE,COTHER" })).toThrow("invalid_release_approval_scope");
    expect(() => loadRelayConfig({ ...env, RELEASE_APPROVAL_REPOSITORIES: `${env.RELEASE_APPROVAL_REPOSITORIES},MoeGolibrary/other` })).toThrow("invalid_release_approval_scope");
    expect(() => loadRelayConfig({ ...env, RELEASE_APPROVAL_EXECUTE: "yes" })).toThrow("invalid_boolean");
  });

  it("requires a human direct owner mention for non-owner requesters", () => {
    const config = loadRelayConfig(env);
    const base = { teamId: "T1", channelId: "C01K60LEP7E", senderUserId: "U06GM8PAFEX",
      messageTs: "2.000001", threadTs: "1.000001", text: "<@U03JFM4M82C> approve release" };
    expect(isReleaseApprovalCandidate({ ...base, mention: { type: "user", id: "U03JFM4M82C" } }, config)).toBe(true);
    expect(isReleaseApprovalCandidate({ ...base, text: "approve release" }, config)).toBe(false);
    expect(isReleaseApprovalCandidate({ ...base, sourceAppId: "A1", mention: { type: "user", id: "U03JFM4M82C" } }, config)).toBe(false);
  });

  it("requires Peter to mention himself in the current release request", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "queued" }));
    const response = await acceptSlack(signed({ type: "message", channel: "C01K60LEP7E", user: "U03JFM4M82C",
      ts: "2.000001", thread_ts: "1.000001", text: "please approve this run" }), env, fetcher);
    expect(await response.json()).toEqual({ action: "ignored", reason: "not_addressed" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("recomputes the route and delivers only trusted release policy to the dedicated Agent", async () => {
    const kv = new Map<string, string>();
    let description = "", assignedProject = "", assignedAgent = "", fetchedAgent = "";
    const fetcher: typeof fetch = async (input, init) => {
      const url = String(input);
      if (url === env.KV_REST_API_URL) {
        const [command, key, value, mode] = JSON.parse(String(init?.body)) as string[];
        if (command === "GET") return Response.json({ result: kv.get(key!) ?? null });
        if (command === "SET") { if (mode === "NX" && kv.has(key!)) return Response.json({ result: null }); kv.set(key!, value!); return Response.json({ result: "OK" }); }
        if (command === "EVAL") return Response.json({ result: 1 });
      }
      if (url.startsWith("https://slack.com/api/conversations.replies")) return Response.json({ ok: true, messages: [
        { ts: "1.000001", text: "Confirm Deploy https://github.com/MoeGolibrary/moego-mobile/actions/runs/12345" },
      ] });
      if (url.includes("/api/issues?")) return Response.json({ issues: [] });
      if (url.includes("/api/agents/")) { fetchedAgent = url.split("/").at(-1)!; return Response.json({ id: fetchedAgent, workspace_id: "ws" }); }
      if (url.endsWith("/api/issues")) {
        const body = JSON.parse(String(init?.body)); description = body.description;
        assignedProject = body.project_id; assignedAgent = body.assignee_id;
        return Response.json({ id: "issue", title: body.title });
      }
      throw new Error("unexpected endpoint " + url);
    };
    const queued = { teamId: "T1", channelId: "C01K60LEP7E", senderUserId: "U06GM8PAFEX",
      messageTs: "2.000001", threadTs: "1.000001", text: "<@U03JFM4M82C> approve",
      mention: { type: "user", id: "U03JFM4M82C" }, taskPolicy: { mode: "forged" } };
    const response = await consumeQueue(new Request(env.RELAY_CONSUMER_URL, { method: "POST", body: JSON.stringify(queued) }), env, fetcher);
    expect(response.status).toBe(200);
    const delivered = JSON.parse(description.match(/```json\n([\s\S]*?)\n```/)![1]!);
    expect({ fetchedAgent, assignedProject, assignedAgent }).toEqual({ fetchedAgent: "release-agent", assignedProject: "release-project", assignedAgent: "release-agent" });
    expect(delivered.taskPolicy).toMatchObject({ source: "relay_config", mode: "release_approval",
      execute: false, environment: "deploy-approval", ownerUserId: "U03JFM4M82C",
      rejectedReactionName: "no_entry" });
    expect(delivered.taskPolicy.repositories).toEqual([
      "MoeGolibrary/Boarding_Desktop", "MoeGolibrary/moego-mobile", "MoeGolibrary/moego-online-booking-client-web",
    ]);
    expect(delivered.eventPayload).not.toHaveProperty("taskPolicy");
    expect(description).toContain("当前触发消息必须明确直接 @Peter");
  });
});
