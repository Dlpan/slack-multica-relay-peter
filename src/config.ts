import { taskFilter, type TaskFilter } from "./task-filter.js";

export interface RelayConfig {
  taskFilter: TaskFilter;
  signingSecret: string;
  teamId: string;
  targetUserIds: Set<string>;
  targetSubteamIds: Set<string>;
  allowedChannelIds: Set<string>;
  allowAllChannels: boolean;
  blockedChannelIds: Set<string>;
  allowedSenderIds: Set<string>;
  allowAllSenders: boolean;
  blockedSenderIds: Set<string>;
  multicaApiBaseUrl: string;
  multicaApiToken: string;
  multicaWorkspaceId: string;
  multicaProjectId: string;
  multicaAgentId: string;
  slackReactionToken: string;
  slackReactionName: string;
  kvRestApiUrl: string;
  kvRestApiToken: string;
  queueUrl: string;
  queueToken: string;
  queueCurrentSigningKey: string;
  queueNextSigningKey: string;
  consumerUrl: string;
}
export function loadRelayConfig(
  env: NodeJS.ProcessEnv = process.env,
): RelayConfig {
  const allowedChannels = policyIds(env.SLACK_ALLOWED_CHANNEL_IDS || "all");
  const blockedChannelIds = ids(env.SLACK_BLOCKED_CHANNEL_IDS);
  const allowedSenders = policyIds(env.SLACK_ALLOWED_SENDER_IDS || "all");
  const blockedSenderIds = ids(env.SLACK_BLOCKED_SENDER_IDS);
  const targetUserIds = ids(env.SLACK_TARGET_USER_IDS);
  const targetSubteamIds = ids(env.SLACK_TARGET_SUBTEAM_IDS);
  if (!targetUserIds.size && !targetSubteamIds.size)
    throw new Error("missing_mention_target");
  return {
    taskFilter: taskFilter(env.SLACK_TASK_FILTER),
    signingSecret: required(env, "SLACK_SIGNING_SECRET"),
    teamId: required(env, "SLACK_TEAM_ID"),
    allowedChannelIds: allowedChannels.ids,
    allowAllChannels: allowedChannels.all,
    blockedChannelIds,
    allowedSenderIds: allowedSenders.ids,
    allowAllSenders: allowedSenders.all,
    blockedSenderIds,
    targetUserIds,
    targetSubteamIds,
    multicaApiBaseUrl: https(required(env, "MULTICA_API_BASE_URL")),
    multicaApiToken: required(env, "MULTICA_API_TOKEN"),
    multicaWorkspaceId: required(env, "MULTICA_WORKSPACE_ID"),
    multicaProjectId: required(env, "MULTICA_PROJECT_ID"),
    multicaAgentId: required(env, "MULTICA_AGENT_ID"),
    slackReactionToken: required(env, "SLACK_REACTION_TOKEN"),
    slackReactionName: required(env, "SLACK_REACTION_NAME").replace(
      /^:+|:+$/gu,
      "",
    ),
    kvRestApiUrl: https(required(env, "KV_REST_API_URL")),
    kvRestApiToken: required(env, "KV_REST_API_TOKEN"),
    queueUrl: https(env.QSTASH_URL?.trim() || "https://qstash.upstash.io"),
    queueToken: required(env, "QSTASH_TOKEN"),
    queueCurrentSigningKey: required(env, "QSTASH_CURRENT_SIGNING_KEY"),
    queueNextSigningKey: required(env, "QSTASH_NEXT_SIGNING_KEY"),
    consumerUrl: https(required(env, "RELAY_CONSUMER_URL")),
  };
}
function required(env: NodeJS.ProcessEnv, key: string): string {
  const value = env[key]?.trim();
  if (!value) throw new Error("relay_not_configured");
  return value;
}
function ids(value: string | undefined): Set<string> {
  const values = (value ?? "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
  if (values.some((x) => !/^[A-Z][A-Z0-9]+$/u.test(x)))
    throw new Error("invalid_identifier");
  return new Set(values);
}
function policyIds(value: string): { ids: Set<string>; all: boolean } {
  const normalized = value.trim();
  if (normalized.toLowerCase() === "all") return { ids: new Set(), all: true };
  const parsed = ids(normalized);
  if (!parsed.size) throw new Error("invalid_allowlist");
  return { ids: parsed, all: false };
}
function https(value: string): string {
  const url = new URL(value);
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.hash ||
    url.search
  )
    throw new Error("invalid_service_url");
  return value.replace(/\/+$/u, "");
}
