import { describe, expect, it } from "vitest";
import { hasThreadReviewContext, isThreadReviewFollowup, isPrReviewRequest, taskFilter } from "../src/task-filter.js";

describe("thread review shorthand", () => {
  it.each(["<!subteam^S1> 再 cc", "<@U1> 帮忙看看", "<@U1> 再 review 一下", "<@U1> 已修改，请复审", "<@U1> please review again"])("allows contextual lookup for %s", text => {
    expect(isThreadReviewFollowup(text)).toBe(true);
    expect(isPrReviewRequest(text)).toBe(false);
  });
  it.each(["<@U1>", "<@U1> FYI", "<@U1> 不用再 cc", "<@U1> 不用 review", "<@U1> 已合并，再 cc", "<@U1> review 已完成", "<@U1> review 设计稿", "<@U1> 帮忙看看报错", "<@U1> 部署后再 cc", "<@U1> `再 cc` 是例子", "<@U1>\n> 再 cc", "<@U1> review https://example.test"])("rejects %s", text => {
    expect(isThreadReviewFollowup(text)).toBe(false);
  });
  it("requires a concrete earlier PR request, excluding quoted or misleading URLs", () => {
    expect(hasThreadReviewContext(["帮忙看看 PR：<https://github.com/org/repo/pull/123>"])).toBe(true);
    for (const text of ["review PR", "review PR `https://github.com/org/repo/pull/123`", "review PR https://github.com.evil.test/org/repo/pull/123", "不用 review https://github.com/org/repo/pull/123", "review 设计稿", "https://github.com/org/repo/pull/123"]) {
      expect(hasThreadReviewContext([text])).toBe(false);
    }
  });
});

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
