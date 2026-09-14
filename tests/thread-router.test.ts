import { afterEach, describe, expect, it, vi } from "vitest";
import {
  routeSlackThreadEvent,
  type SlackThreadEvent,
  type ThreadRouterConfig,
} from "../src/thread-router.js";
import { MemoryThreadStore } from "../src/thread-store.js";
const root: SlackThreadEvent = {
  teamId: "T1",
  channelId: "C1",
  messageTs: "100.000001",
  threadTs: "100.000001",
  senderUserId: "U2",
  text: "<@U1> test",
  mention: { type: "user", id: "U1" },
};
function fixture() {
  const issues: {
    id: string;
    title: string;
    description: string;
    project_id: string;
    assignee_type: string;
    assignee_id: string;
  }[] = [];
  const comments: { id: string; content: string }[] = [];
  let issuePosts = 0,
    agentGets = 0,
    commentPosts = 0,
    failIssue = false,
    failComment = false;
  const agentConfig = { model: "gpt-6-astra", service_tier: "priority" };
  const fetcher: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.includes("/api/agents/")) {
      agentGets++;
      return Response.json({
        id: decodeURIComponent(url.split("/").at(-1)!),
        workspace_id: "ws",
        ...agentConfig,
      });
    }
    if (url.includes("/api/issues?")) return Response.json({ issues });
    if (url.endsWith("/api/issues")) {
      issuePosts++;
      const data = JSON.parse(String(init?.body));
      const row = {
        id: "issue-" + issuePosts,
        title: data.title,
        description: data.description,
        project_id: "project",
        assignee_type: "agent",
        assignee_id: "agent",
      };
      issues.push(row);
      if (failIssue) throw new DOMException("lost response", "TimeoutError");
      return Response.json(row, { status: 201 });
    }
    if (url.includes("/comments")) {
      if (init?.method === "POST") {
        commentPosts++;
        const row = {
          id: "comment-" + commentPosts,
          content: JSON.parse(String(init.body)).content,
        };
        comments.push(row);
        if (failComment)
          throw new DOMException("lost response", "TimeoutError");
        return Response.json(row, { status: 201 });
      }
      return Response.json(comments);
    }
    throw new Error("unexpected endpoint");
  };
  const config: ThreadRouterConfig = {
    multicaApiBaseUrl: "https://multica.test",
    multicaApiToken: "test",
    multicaWorkspaceId: "ws",
    multicaProjectId: "project",
    multicaAgentId: "agent",
    store: new MemoryThreadStore(),
  };
  return {
    config,
    fetcher,
    issues,
    comments,
    agentConfig,
    get agentGets() {
      return agentGets;
    },
    get issuePosts() {
      return issuePosts;
    },
    get commentPosts() {
      return commentPosts;
    },
    loseIssueResponse() {
      failIssue = true;
    },
    loseCommentResponse() {
      failComment = true;
    },
  };
}
afterEach(() => vi.restoreAllMocks());
describe("direct Issue routing", () => {
  function payload(description: string) {
    return JSON.parse(description.match(/```json\n([\s\S]*?)\n```/)![1]!);
  }

  it("applies the current PR-only scope to an existing thread without duplicating a message", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    expect(payload(f.issues[0]!.description)).not.toHaveProperty("taskPolicy");
    f.config.taskFilter = "pr_review";
    f.config.slackReactionName = "eyes";
    const next = { ...root, messageTs: "102.000001", text: "<@U1> 已按你的建议调整", taskPolicy: { mode: "all" } };
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
    expect(payload(f.comments[0]!.content)).toMatchObject({
      eventPayload: { text: next.text, threadTs: root.threadTs },
      taskPolicy: { source: "relay_config", mode: "pr_review", reactionName: "eyes" },
    });
    expect(payload(f.comments[0]!.content).eventPayload).not.toHaveProperty("taskPolicy");
  });

  it("attaches a fresh configuration snapshot to each new message, not each duplicate", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    expect(payload(f.issues[0]!.description).replyContext).toMatchObject({
      type: "slack_reply_context",
      source: "agent_config",
      status: "available",
      agentId: "agent",
      model: "gpt-6-astra",
      serviceTier: "priority",
    });
    f.agentConfig.model = "gpt-5.6-sol";
    f.agentConfig.service_tier = "default";
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    const next = { ...root, messageTs: "102.000001" };
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(payload(f.comments[0]!.content)).toMatchObject({
      eventPayload: { messageTs: next.messageTs },
      replyContext: { model: "gpt-5.6-sol", serviceTier: "default" },
    });
    expect(payload(f.issues[0]!.description).replyContext.model).toBe("gpt-6-astra");
    expect(f.agentGets).toBe(2);
  });

  it("continues both initial and followup writes when Agent configuration times out", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = fixture();
    const fetcher: typeof fetch = async (input, init) => {
      if (String(input).includes("/api/agents/"))
        throw new DOMException("timeout", "TimeoutError");
      return f.fetcher(input, init);
    };
    expect((await routeSlackThreadEvent(root, f.config, fetcher)).action).toBe("created");
    const next = { ...root, messageTs: "102.000001" };
    expect((await routeSlackThreadEvent(next, f.config, fetcher)).action).toBe("comment_persisted");
    for (const text of [f.issues[0]!.description, f.comments[0]!.content]) {
      expect(payload(text).replyContext).toMatchObject({
        status: "unavailable", model: null, serviceTier: null,
      });
      expect(payload(text).eventPayload.text).toBe(root.text);
    }
  });
  it("creates distinct issues for two simultaneous different Slack threads", async () => {
    const f = fixture();
    await Promise.all([
      routeSlackThreadEvent(root, f.config, f.fetcher),
      routeSlackThreadEvent(
        { ...root, messageTs: "101.000001", threadTs: "101.000001" },
        f.config,
        f.fetcher,
      ),
    ]);
    expect(f.issuePosts).toBe(2);
    expect(f.issues[0]!.title).not.toBe(f.issues[1]!.title);
  });
  it("one thread creates once and appends once per followup", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    expect(
      (await routeSlackThreadEvent(root, f.config, f.fetcher)).action,
    ).toBe("duplicate");
    const next = { ...root, messageTs: "102.000001" };
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.agentGets).toBe(2);
    expect(f.commentPosts).toBe(1);
  });
  it("recovers a committed Issue after lost response without second POST", async () => {
    const f = fixture();
    f.loseIssueResponse();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow();
    expect(
      (await routeSlackThreadEvent(root, f.config, f.fetcher)).issueId,
    ).toBe("issue-1");
    expect(f.issuePosts).toBe(1);
    expect(f.agentGets).toBe(1);
  });
  it("does not blindly replay an ambiguous unconfirmed Issue POST", async () => {
    const f = fixture();
    f.loseIssueResponse();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow();
    f.issues.splice(0);
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("ambiguous_issue_create");
    expect(f.issuePosts).toBe(1);
  });
  it("recovers a committed comment after lost response without duplicate followup", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.loseCommentResponse();
    const next = { ...root, messageTs: "102.000001" };
    await expect(
      routeSlackThreadEvent(next, f.config, f.fetcher),
    ).rejects.toThrow();
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(f.commentPosts).toBe(1);
    expect(f.agentGets).toBe(2);
  });
  it("recovers original root identity after KV mapping expires", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.config.store = new MemoryThreadStore();
    const result = await routeSlackThreadEvent(
      { ...root, messageTs: "102.000001" },
      f.config,
      f.fetcher,
    );
    expect(result.action).toBe("comment_persisted");
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it("does not POST while another worker owns the lock", async () => {
    const f = fixture();
    f.config.store.setIfAbsent = async () => false;
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("thread_lock_busy");
    expect(f.issuePosts).toBe(0);
    expect(f.agentGets).toBe(0);
  });
  it("recovers a legacy description after KV expiry without creating a second Issue", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    const row = f.issues[0]!;
    row.description =
      row.description.split("\n")[0] +
      "\n" +
      JSON.stringify({ eventPayload: root });
    f.config.store = new MemoryThreadStore();
    await routeSlackThreadEvent(
      { ...root, messageTs: "102.000001" },
      f.config,
      f.fetcher,
    );
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it.each(["damaged", "mismatched"])(
    "does not recreate an Issue with %s recovery data",
    async (mode) => {
      const f = fixture();
      await routeSlackThreadEvent(root, f.config, f.fetcher);
      f.issues[0]!.description =
        mode === "damaged"
          ? f.issues[0]!.description.replace("<!-- /relay-payload -->", "")
          : f.issues[0]!.description.replace(
              '"channelId": "C1"',
              '"channelId": "C2"',
            );
      f.config.store = new MemoryThreadStore();
      await expect(
        routeSlackThreadEvent(root, f.config, f.fetcher),
      ).rejects.toThrow("invalid_thread_state");
      expect(f.issuePosts).toBe(1);
      expect(f.commentPosts).toBe(0);
    },
  );
  it("retains both requests when a rejected first create is overtaken by a followup", async () => {
    const f = fixture();
    let reject = true;
    const fetcher: typeof fetch = async (input, init) => {
      if (reject && String(input).endsWith("/api/issues")) {
        reject = false;
        return Response.json({ error: "rate_limited" }, { status: 429 });
      }
      return f.fetcher(input, init);
    };
    await expect(
      routeSlackThreadEvent(root, f.config, fetcher),
    ).rejects.toThrow();
    await routeSlackThreadEvent(
      { ...root, messageTs: "102.000001", text: "<@U1> B" },
      f.config,
      fetcher,
    );
    await routeSlackThreadEvent(root, f.config, fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
    expect(f.issues[0]!.description).toContain("<@U1> B");
    expect(f.comments[0]!.content).toContain("<@U1> test");
  });
  it("does not adopt a different configured Agent scope", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    await routeSlackThreadEvent(
      root,
      { ...f.config, multicaAgentId: "another-agent" },
      f.fetcher,
    );
    expect(f.issuePosts).toBe(2);
  });
  it("does not recover an Issue assigned to another Agent", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.issues[0]!.assignee_id = "another-agent";
    f.config.store = new MemoryThreadStore();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("invalid_issue_scope");
    expect(f.issuePosts).toBe(1);
  });
});
