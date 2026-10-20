export type TaskFilter = "all" | "pr_review" | "pr_review_and_ticket";

export function taskFilter(value: string | undefined): TaskFilter {
  const filter = value?.trim() || "all";
  if (filter !== "all" && filter !== "pr_review" && filter !== "pr_review_and_ticket")
    throw new Error("invalid_task_filter");
  return filter;
}
