import { describe, expect, it } from "vitest";
import { isPrReviewRequest, taskFilter } from "../src/task-filter.js";

describe("PR review admission", () => {
  it.each([
    "<@U1> please review PR #123",
    "<@U1> PR review 一下",
    "<@U1> 帮忙 review 这个 PR",
    "<@U1> 请审查这个合并请求",
    "<@U1> CR https://github.com/org/repo/pull/12",
    "<@U1> 帮看下 <https://github.com/org/repo/pull/12|PR>",
    "<@U1> 帮忙看一下 https://github.com/org/repo/pull/12/files",
    "<@U1> review https://github.com/org/repo/pull/12#discussion_r1",
    "<@U1> Please review this pull request",
    "<@U1> 重新 review PR #123",
  ])("admits %s", text => expect(isPrReviewRequest(text)).toBe(true));
  it.each([
    "<@U1> 测试路由", "<@U1> review 设计稿", "<@U1> 帮忙看看报错",
    "<@U1> 合并 PR #123", "<@U1> PR 已发布", "<@U1> https://github.com/org/repo/pull/12",
    "<@U1> 不用 review PR #123", "<@U1> 不需要你 review PR #123",
    "<@U1> don't review PR #123", "<@U1> no need to review PR #123",
    "<@U1> PR review 已完成", "<@U1> PR review is done", "<@U1> 已经review完 PR #123",
    "<@U1> reviewed PR #123", "<@U1> review https://github.com/org/repo/issues/12",
    "<@U1> review https://github.com.evil.test/org/repo/pull/12",
    "<@U1> review https://github.com@evil.test/org/repo/pull/12",
    "<@U1> <https://example.test|review PR>",
    "<@U1> `review PR #123` 是示例", "<@U1>\n> review PR #123\n只是转述",
    "<@U1>\n```review PR #123```\n只是示例",
    "<@U1> preview sprint", "<@U1> https://example.test/review-pr",
  ])("ignores %s", text => expect(isPrReviewRequest(text)).toBe(false));
  it("validates configuration", () => {
    expect(taskFilter(undefined)).toBe("all");
    expect(taskFilter(" pr_review ")).toBe("pr_review");
    expect(() => taskFilter("reviews")).toThrow("invalid_task_filter");
  });
});
