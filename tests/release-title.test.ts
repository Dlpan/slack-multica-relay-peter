import { describe, expect, it, vi } from "vitest";
import {
  parseReleaseTaskTitleContext,
  readReleaseTaskTitleContext,
} from "../src/release-title.js";

const event = {
  teamId: "T1", channelId: "C1", senderUserId: "U1",
  messageTs: "2.000001", threadTs: "1.000001", text: "<@U1> approve",
};

describe("release title", () => {
  it("extracts one repository and run from Slack-formatted root text", () => {
    expect(parseReleaseTaskTitleContext(
      "<https://github.com/MoeGolibrary/moego-mobile/actions/runs/12345|release>",
    )).toEqual({ repository: "MoeGolibrary/moego-mobile", runId: "12345" });
    expect(parseReleaseTaskTitleContext(
      "https://github.com/MoeGolibrary/a/actions/runs/1 https://github.com/MoeGolibrary/b/actions/runs/2",
    )).toBeUndefined();
  });

  it("reads only the root message for presentation context", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ok: true, messages: [
      { ts: event.threadTs, text: "Confirm https://github.com/MoeGolibrary/Boarding_Desktop/actions/runs/42" },
    ] }));
    await expect(readReleaseTaskTitleContext("token", event, fetcher)).resolves.toEqual({
      repository: "MoeGolibrary/Boarding_Desktop", runId: "42",
    });
    const [input, init] = fetcher.mock.calls[0]!;
    expect(String(input)).toContain("conversations.replies");
    expect(init?.headers).toEqual({ authorization: "Bearer token" });
  });
});
