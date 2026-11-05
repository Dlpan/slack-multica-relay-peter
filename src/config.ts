import { taskFilter, type TaskFilter } from "./task-filter.js";

const RELEASE_OWNER = "U03JFM4M82C";
const RELEASE_REQUESTERS = new Set(["U06GM8PAFEX", RELEASE_OWNER, "U06RHAA209Y"]);
const RELEASE_CHANNELS = new Set(["C01K60LEP7E"]);
const RELEASE_ROOT_BOT_USERS = new Set(["U081137G64T"]);
const RELEASE_ROOT_BOTS = new Set(["B081PVALKC0"]);
const RELEASE_REPOSITORIES = new Set([
  "MoeGolibrary/moego-online-booking-client-web",
  "MoeGolibrary/Boarding_Desktop",
  "MoeGolibrary/moego-mobile",
]);

export interface RelayConfig {
  taskFilter: TaskFilter;
  ticketOwnerUserId?: string;
  allowedAppActors: Set<string>;
  allowedBotIds: Set<string>;
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
  releaseApproval?: ReleaseApprovalConfig;
}

export interface ReleaseApprovalConfig {
  ownerUserId: string;
  requesterUserIds: Set<string>;
  channelIds: Set<string>;
  rootBotUserIds: Set<string>;
  rootBotIds: Set<string>;
  repositories: Set<string>;
  environment: "deploy-approval";
  execute: boolean;
  multicaProjectId: string;
  multicaAgentId: string;
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
  const filter = taskFilter(env.SLACK_TASK_FILTER);
  const ticketOwnerUserId = env.RELAY_OWNER_SLACK_USER_ID?.trim();
  if (filter === "pr_review_and_ticket" &&
      (!ticketOwnerUserId || !/^[UW][A-Z0-9]+$/u.test(ticketOwnerUserId) || !targetUserIds.has(ticketOwnerUserId)))
    throw new Error("invalid_ticket_owner");
  const releaseApproval = releaseApprovalConfig(env);
  return {
    taskFilter: filter,
    ticketOwnerUserId,
    allowedAppActors: appActors(env.SLACK_ALLOWED_APP_ACTORS),
    allowedBotIds: botIds(env.SLACK_ALLOWED_BOT_IDS),
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
    releaseApproval,
  };
}

function releaseApprovalConfig(env: NodeJS.ProcessEnv): ReleaseApprovalConfig | undefined {
  const enabled = boolean(env.RELEASE_APPROVAL_ENABLED, false);
  if (!enabled) return undefined;
  const ownerUserId = required(env, "RELEASE_APPROVAL_OWNER_USER_ID");
  const requesterUserIds = ids(required(env, "RELEASE_APPROVAL_REQUESTER_USER_IDS"));
  const channelIds = ids(required(env, "RELEASE_APPROVAL_CHANNEL_IDS"));
  const rootBotUserIds = ids(required(env, "RELEASE_APPROVAL_ROOT_BOT_USER_IDS"));
  const rootBotIds = botIds(required(env, "RELEASE_APPROVAL_ROOT_BOT_IDS"));
  const repositories = repositoriesList(required(env, "RELEASE_APPROVAL_REPOSITORIES"));
  if (ownerUserId !== RELEASE_OWNER || !sameSet(requesterUserIds, RELEASE_REQUESTERS))
    throw new Error("invalid_release_approval_owner");
  if (!sameSet(channelIds, RELEASE_CHANNELS) || !sameSet(rootBotUserIds, RELEASE_ROOT_BOT_USERS) ||
      !sameSet(rootBotIds, RELEASE_ROOT_BOTS) || !sameSet(repositories, RELEASE_REPOSITORIES))
    throw new Error("invalid_release_approval_scope");
  const environment = required(env, "RELEASE_APPROVAL_ENVIRONMENT");
  if (environment !== "deploy-approval") throw new Error("invalid_release_approval_environment");
  return {
    ownerUserId,
    requesterUserIds,
    channelIds,
    rootBotUserIds,
    rootBotIds,
    repositories,
    environment,
    execute: boolean(env.RELEASE_APPROVAL_EXECUTE, false),
    multicaProjectId: required(env, "MULTICA_RELEASE_APPROVAL_PROJECT_ID"),
    multicaAgentId: required(env, "MULTICA_RELEASE_APPROVAL_AGENT_ID"),
  };
}
function required(env: NodeJS.ProcessEnv, key: string): string {
  const value = env[key]?.trim();
  if (!value) throw new Error("relay_not_configured");
  return value;
}
function appActors(value: string | undefined): Set<string> {
  const entries = (value ?? "").split(",").map(x => x.trim()).filter(Boolean);
  if (entries.some(x => !/^A[A-Z0-9]+:[UW][A-Z0-9]+$/u.test(x)))
    throw new Error("invalid_app_actor");
  return new Set(entries);
}
function botIds(value: string | undefined): Set<string> {
  const entries = (value ?? "").split(",").map(x => x.trim()).filter(Boolean);
  if (entries.some(x => !/^B[A-Z0-9]+$/u.test(x)))
    throw new Error("invalid_bot_id");
  return new Set(entries);
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
function repositoriesList(value: string): Set<string> {
  const values = value.split(",").map((x) => x.trim()).filter(Boolean);
  if (!values.length || values.some((x) => !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/u.test(x)))
    throw new Error("invalid_repository_allowlist");
  return new Set(values);
}
function boolean(value: string | undefined, fallback: boolean): boolean {
  const normalized = value?.trim().toLowerCase();
  if (!normalized) return fallback;
  if (normalized === "true") return true;
  if (normalized === "false") return false;
  throw new Error("invalid_boolean");
}
function sameSet(left: Set<string>, right: Set<string>): boolean {
  return left.size === right.size && [...left].every((value) => right.has(value));
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
