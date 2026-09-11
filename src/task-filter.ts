export type TaskFilter = "all" | "pr_review";

export function taskFilter(value: string | undefined): TaskFilter {
  const filter = value?.trim() || "all";
  if (filter !== "all" && filter !== "pr_review")
    throw new Error("invalid_task_filter");
  return filter;
}

/** Conservative admission filter; the Agent still validates the actual request. */
export function isPrReviewRequest(text: string): boolean {
  // Examples and quoted messages must not turn ordinary mentions into tasks.
  const unquoted = text.replace(/```[\s\S]*?```/gu, " ")
    .replace(/`[^`\n]*`/gu, " ")
    .replace(/^\s*(?:>|&gt;).*$/gmu, " ");
  let hasPullUrl = false;
  const words = unquoted.replace(/<https?:\/\/[^>]+>|https?:\/\/[^\s<>]+/giu, (link) => {
    const raw = link.startsWith("<") ? link.slice(1, -1).split("|")[0]! : link;
    try {
      const url = new URL(raw);
      hasPullUrl ||= url.protocol === "https:" && url.hostname === "github.com"
        && !url.username && !url.password
        && /^\/[^/]+\/[^/]+\/pull\/\d+(?:\/|$)/u.test(url.pathname);
    } catch { /* An invalid link is not PR context. */ }
    return " ";
  }).replace(/<[^>]*>/gu, " ");
  const hasPr = hasPullUrl || /\b(?:pr|pull[\s-]*request)\b|拉取请求|合并请求/iu.test(words);
  if (!hasPr) return false;
  // Do not act on explicit opt-outs or completed-review announcements.
  if (/(?:不要|不用|无需|不需要|别).{0,12}(?:review|\bcr\b|审查|评审|审核|看)|\b(?:do\s+not|don't|no\s+need\s+to|skip)\s+(?:\w+\s+){0,2}(?:review|cr)\b/iu.test(words)) return false;
  if (/(?:review|评审|审查|审核)\s*(?:已完成|完成了|完了)|(?:已经|已)\s*(?:review|评审|审查|审核)(?:过|完|完成)|\breview\s+(?:is\s+)?(?:done|completed)\b/iu.test(words)) return false;
  return /\b(?:review|cr)\b|审查|评审|审核|(?:帮忙|帮我|麻烦|请)?\s*(?:看下|看一下|看看|过目)/iu.test(words);
}
