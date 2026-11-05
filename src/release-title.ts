import type { SlackThreadEvent } from "./thread-router.js";

export interface ReleaseTaskTitleContext {
  repository: string;
  runId: string;
}

const RUN_URL = /https:\/\/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\/actions\/runs\/([0-9]+)(?=[/?#|>\s]|$)/gu;

export function parseReleaseTaskTitleContext(text: string): ReleaseTaskTitleContext | undefined {
  const matches = [...text.matchAll(RUN_URL)].map((match) => ({
    repository: match[1]!,
    runId: match[2]!,
  }));
  const unique = new Map(matches.map((match) => [`${match.repository}:${match.runId}`, match]));
  return unique.size === 1 ? [...unique.values()][0] : undefined;
}

export async function readReleaseTaskTitleContext(
  token: string,
  event: SlackThreadEvent,
  fetchImpl: typeof fetch = fetch,
): Promise<ReleaseTaskTitleContext | undefined> {
  const url = new URL("https://slack.com/api/conversations.replies");
  url.search = new URLSearchParams({
    channel: event.channelId,
    ts: event.threadTs,
    limit: "1",
    inclusive: "true",
  }).toString();
  const response = await fetchImpl(url, {
    headers: { authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(2_000),
  });
  if (!response.ok) throw new Error("slack_root_read_failed");
  const body = await response.json() as { ok?: unknown; messages?: unknown };
  if (body.ok !== true || !Array.isArray(body.messages))
    throw new Error("slack_root_read_failed");
  const root = body.messages.find((message) =>
    !!message && typeof message === "object" &&
    (message as Record<string, unknown>).ts === event.threadTs,
  ) as Record<string, unknown> | undefined;
  return typeof root?.text === "string"
    ? parseReleaseTaskTitleContext(root.text)
    : undefined;
}
