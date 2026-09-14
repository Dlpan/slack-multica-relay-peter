export type TaskFilter = "all" | "pr_review";

export function taskFilter(value: string | undefined): TaskFilter {
  const filter = value?.trim() || "all";
  if (filter !== "all" && filter !== "pr_review")
    throw new Error("invalid_task_filter");
  return filter;
}

/** Conservative admission filter; the Agent still validates the actual request. */
function reviewText(text: string): { words: string; hasPullUrl: boolean } {
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
  return { words, hasPullUrl };
}

export function isPrReviewRequest(text: string): boolean {
  const { words, hasPullUrl } = reviewText(text);
  const hasPr = hasPullUrl || /\b(?:pr|pull[\s-]*request)\b|拉取请求|合并请求/iu.test(words);
  if (!hasPr) return false;
  // Do not act on explicit opt-outs or completed-review announcements.
  if (/(?:不要|不用|无需|不需要|别).{0,12}(?:review|\bcr\b|审查|评审|审核|看)|\b(?:do\s+not|don't|no\s+need\s+to|skip)\s+(?:\w+\s+){0,2}(?:review|cr)\b/iu.test(words)) return false;
  if (/(?:review|评审|审查|审核)\s*(?:已完成|完成了|完了)|(?:已经|已)\s*(?:review|评审|审查|审核)(?:过|完|完成)|\breview\s+(?:is\s+)?(?:done|completed)\b/iu.test(words)) return false;
  return /\b(?:review|cr)\b|审查|评审|审核|(?:帮忙|帮我|麻烦|请)?\s*(?:看下|看一下|看看|过目)/iu.test(words);
}

/** Only short review hand-offs can borrow a target from their own thread. */
export function isThreadReviewFollowup(text: string): boolean {
  // Do not turn quoted requests or a different linked object into a review.
  if (/https?:\/\//iu.test(text)) return false;
  const words = text.replace(/```[\s\S]*?```/gu, " ")
    .replace(/`[^`\n]*`/gu, " ")
    .replace(/^\s*(?:>|&gt;).*$/gmu, " ")
    .replace(/<[^>]*>/gu, " ").trim()
    .replace(/[，,。.!！?？]+$/gu, "").trim();
  return /^(?:(?:已修改|已修复|修好了|改好了|已处理\s*comments?)\s*[,，。]?\s*)?(?:(?:麻烦|请|帮忙|帮我)\s*)?(?:(?:再|重新|再次)\s*)?(?:cc|review|cr|复审|审查|评审|审核|看看|看下|看一下|过目)(?:\s*(?:一下|下|一遍|一次|again))?$/iu.test(words)
    || /^(?:please\s+)?(?:review|re-review)(?:\s+(?:this|again))*$/iu.test(words);
}

/** A concrete PR request in earlier human messages anchors shorthand replies. */
export function hasThreadReviewContext(texts: string[]): boolean {
  // Re-use the same quote/link/opt-out handling as standalone admission.
  return texts.some(text => isPrReviewRequest(text)
    && reviewText(text).hasPullUrl);
}
