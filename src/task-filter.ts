export type TaskFilter = "all" | "pr_review";

export function taskFilter(value: string | undefined): TaskFilter {
  const filter = value?.trim() || "all";
  if (filter !== "all" && filter !== "pr_review")
    throw new Error("invalid_task_filter");
  return filter;
}
