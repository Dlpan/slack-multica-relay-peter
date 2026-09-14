import { describe, expect, it } from "vitest";
import { taskFilter } from "../src/task-filter.js";

describe("execution scope configuration", () => {
  it("validates supported modes without interpreting message text", () => {
    expect(taskFilter(undefined)).toBe("all");
    expect(taskFilter(" pr_review ")).toBe("pr_review");
    expect(() => taskFilter("reviews")).toThrow("invalid_task_filter");
  });
});
