export type MentionType = 'user' | 'subteam';

export interface MentionMatch {
  type: MentionType;
  id: string;
}

const USER_MENTION = /<@([A-Z0-9]+)(?:\|[^>]+)?>/gu;
const SUBTEAM_MENTION = /<!subteam\^([A-Z0-9]+)(?:\|[^>]+)?>/gu;

export function findTargetMention(
  text: string,
  targetUserIds: ReadonlySet<string>,
  targetSubteamIds: ReadonlySet<string>,
): MentionMatch | undefined {
  for (const match of text.matchAll(USER_MENTION)) {
    if (targetUserIds.has(match[1]!)) return { type: 'user', id: match[1]! };
  }
  for (const match of text.matchAll(SUBTEAM_MENTION)) {
    if (targetSubteamIds.has(match[1]!)) return { type: 'subteam', id: match[1]! };
  }
  return undefined;
}

export function isSupportedMessage(
  event: SlackMessageEvent,
  allowedAppActors: ReadonlySet<string> = new Set(),
  allowedBotIds: ReadonlySet<string> = new Set(),
): boolean {
  const subtype = typeof event.subtype === 'string' ? event.subtype : undefined;
  const appActorAllowed = typeof event.app_id === 'string'
    && typeof event.user === 'string'
    && allowedAppActors.has(`${event.app_id}:${event.user}`);
  const botAllowed = typeof event.bot_id === 'string'
    && allowedBotIds.has(event.bot_id);
  const applicationMarked = !!event.bot_id || !!event.app_id || subtype === 'bot_message';
  return event.type === 'message'
    && typeof event.channel === 'string'
    && typeof event.ts === 'string'
    && typeof event.text === 'string'
    && !['message_changed', 'message_deleted'].includes(subtype ?? '')
    && (subtype === 'bot_message'
      ? botAllowed
      : (!applicationMarked || appActorAllowed || botAllowed));
}

export interface SlackMessageEvent {
  type?: unknown;
  team?: unknown;
  channel?: unknown;
  channel_type?: unknown;
  ts?: unknown;
  thread_ts?: unknown;
  user?: unknown;
  text?: unknown;
  subtype?: unknown;
  bot_id?: unknown;
  app_id?: unknown;
  files?: unknown;
}
