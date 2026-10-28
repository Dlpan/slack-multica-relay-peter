import { describe, expect, it } from "vitest";
import { routeSlackThreadEvent, type SlackThreadEvent } from "../src/thread-router.js";
import { loadRelayConfig } from "../src/config.js";
import { taskFilter } from "../src/task-filter.js";
import { MemoryThreadStore } from "../src/thread-store.js";

const event: SlackThreadEvent = { teamId: "T1", channelId: "C1", senderUserId: "U1",
  messageTs: "100.000001", threadTs: "100.000001", text: "<@U1> close ENG-123",
  mention: { type: "user", id: "U1" } };
const config = { multicaApiBaseUrl: "https://multica.test", multicaApiToken: "test", multicaWorkspaceId: "ws",
  multicaProjectId: "project", multicaAgentId: "agent", taskFilter: "pr_review_and_ticket" as const, ticketOwnerUserId: "U1" };

describe("per-message owner ticket policy", () => {
  it.each([
    ["owner direct", {}, "pr_review_and_ticket"],
    ["other sender", { senderUserId: "U2" }, "pr_review"],
    ["owner app", { sourceAppId: "A1" }, "pr_review"],
    ["owner bot", { sourceBotId: "B1" }, "pr_review"],
    ["group alone", { text: "<!subteam^S1> close ENG-123", mention: { type: "subteam", id: "S1" } }, "pr_review"],
    ["both group and owner", { text: "<!subteam^S1> <@U1> close ENG-123", mention: { type: "subteam", id: "S1" } }, "pr_review_and_ticket"],
  ])("%s", async (_name, change, mode) => {
    let description = "";
    const fetcher: typeof fetch = async (input, init) => {
      const url = String(input);
      if (url.includes("/api/issues?")) return Response.json({ issues: [] });
      if (url.endsWith("/api/agents/agent")) return Response.json({ id: "agent", workspace_id: "ws" });
      if (url.endsWith("/api/issues")) {
        description = JSON.parse(String(init?.body)).description;
        return Response.json({ id: "issue", title: "ticket task" });
      }
      throw new Error("unexpected endpoint " + url);
    };
    await routeSlackThreadEvent({ ...event, ...change } as SlackThreadEvent,
      { ...config, store: new MemoryThreadStore() }, fetcher);
    const payload = JSON.parse(description.match(/```json\n([\s\S]*?)\n```/)![1]!);
    expect(payload.taskPolicy.mode).toBe(mode);
    expect(payload.taskPolicy.ticketOwnerUserId).toBe(mode === "pr_review_and_ticket" ? "U1" : undefined);
    expect(payload.eventPayload.sourceAppId).toBe((change as Partial<SlackThreadEvent>).sourceAppId);
    if (mode === "pr_review_and_ticket") {
      expect(payload.taskPolicy.ticketReactionName).toBe("ack");
      expect(payload.taskPolicy.ticketCompletedReactionName).toBe("white_check_mark");
    }
  });
  it("keeps PR-only mode unchanged even for owner", async () => {
    expect(taskFilter("pr_review_and_ticket")).toBe("pr_review_and_ticket");
    expect(() => loadRelayConfig({ SLACK_TASK_FILTER: "pr_review_and_ticket", SLACK_TARGET_USER_IDS: "U1" }))
      .toThrow("invalid_ticket_owner");
    expect(() => loadRelayConfig({ SLACK_TASK_FILTER: "pr_review_and_ticket", SLACK_TARGET_USER_IDS: "U1",
      RELAY_OWNER_SLACK_USER_ID: "U2" })).toThrow("invalid_ticket_owner");
  });
});
