import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "multica-skills/github-release-approval/scripts/release_approval.py"
SPEC = importlib.util.spec_from_file_location("release_approval", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(release)


def event(sender="U06GM8PAFEX", text="<@U03JFM4M82C> approve this release"):
    return {"teamId": "T1", "channelId": release.CHANNEL, "messageTs": "2.000001",
            "threadTs": "1.000001", "senderUserId": sender, "text": text}


def thread(value):
    return [
        {"ts": "1.000001", "user": release.ROOT_BOT_USER, "bot_id": release.ROOT_BOT_ID,
         "text": "Confirm Deploy https://github.com/MoeGolibrary/moego-mobile/actions/runs/12345"},
        {"ts": "2.000001", "user": value["senderUserId"], "text": value["text"]},
    ]


def github_response(path, **kwargs):
    if path == "user": return {"login": "Dlpan"}
    if path.endswith("/actions/runs/12345"):
        return {"status": "in_progress", "conclusion": None, "workflow_id": 9,
                "html_url": "https://github.com/MoeGolibrary/moego-mobile/actions/runs/12345",
                "repository": {"full_name": "MoeGolibrary/moego-mobile"}}
    if path.endswith("/actions/workflows/9"):
        return {"name": release.WORKFLOW_NAME, "path": release.WORKFLOW_PATH}
    if path.endswith("/pending_deployments"):
        return [{"environment": {"id": 7, "name": release.ENVIRONMENT}, "current_user_can_approve": True}]
    raise AssertionError(path)


class ReleaseApprovalTest(unittest.TestCase):
    def test_trusted_human_thread_resolves_one_allowed_run(self):
        value = event()
        with patch.object(release, "read_thread", return_value=thread(value)):
            self.assertEqual(release.verify_slack(value), ("MoeGolibrary/moego-mobile", 12345))

    def test_non_owner_requires_direct_mention_and_app_origin_never_authorizes(self):
        for changed, code in [
            ({"text": "approve this release"}, "missing_owner_mention"),
            ({"sourceAppId": "A123"}, "non_human_requester"),
            ({"senderUserId": "UUNKNOWN"}, "unauthorized_requester"),
        ]:
            value = {**event(), **changed}
            live_thread = thread(value)
            with patch.object(release, "read_thread", return_value=live_thread):
                with self.assertRaisesRegex(release.ApprovalError, code):
                    release.verify_slack(value)

    def test_owner_can_request_in_thread_without_self_mention(self):
        value = event(release.OWNER, "approve this production run")
        with patch.object(release, "read_thread", return_value=thread(value)):
            self.assertEqual(release.verify_slack(value)[1], 12345)

    def test_root_bot_repository_workflow_and_environment_fail_closed(self):
        value = event()
        bad = thread(value)
        bad[0]["bot_id"] = "BOTHER"
        with patch.object(release, "read_thread", return_value=bad):
            with self.assertRaisesRegex(release.ApprovalError, "untrusted_root_bot"):
                release.verify_slack(value)
        with patch.object(release, "gh_json", side_effect=lambda path, **kwargs:
                          {"name": "Other", "path": release.WORKFLOW_PATH}
                          if path.endswith("/actions/workflows/9") else github_response(path, **kwargs)):
            with self.assertRaisesRegex(release.ApprovalError, "workflow_not_allowed"):
                release.verify_github("MoeGolibrary/moego-mobile", 12345)
        with patch.object(release, "gh_json", side_effect=lambda path, **kwargs:
                          [{"environment": {"id": 7, "name": "production"}, "current_user_can_approve": True}]
                          if path.endswith("/pending_deployments") else github_response(path, **kwargs)):
            with self.assertRaisesRegex(release.ApprovalError, "environment_not_allowed"):
                release.verify_github("MoeGolibrary/moego-mobile", 12345)

    def test_live_write_is_exact_idempotent_and_read_back(self):
        value = event()
        calls = []
        pending_reads = 0

        def api(path, **kwargs):
            nonlocal pending_reads
            calls.append((path, kwargs))
            if kwargs.get("method") == "POST": return [{"state": "approved"}]
            if path.endswith("/pending_deployments"):
                pending_reads += 1
                return []
            return github_response(path, **kwargs)

        run = github_response("repos/MoeGolibrary/moego-mobile/actions/runs/12345")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "RELEASE_APPROVAL_EXECUTE": "true", "RELEASE_APPROVAL_STATE_DIR": directory,
        }, clear=False), patch.object(release, "gh_json", side_effect=api):
            result = release.execute(value, "MoeGolibrary/moego-mobile", 12345, 7, run)
            self.assertEqual(result["outcome"], "approved")
            self.assertTrue(result["verified"])
            post = next(item for item in calls if item[1].get("method") == "POST")
            self.assertEqual(post[1]["payload"]["environment_ids"], [7])
            self.assertEqual(post[1]["payload"]["state"], "approved")
            again = release.execute(value, "MoeGolibrary/moego-mobile", 12345, 7, run)
            self.assertEqual(again["outcome"], "already_verified")
            self.assertEqual(sum(1 for item in calls if item[1].get("method") == "POST"), 1)
            self.assertEqual(pending_reads, 1)

    def test_live_execution_defaults_off(self):
        value = event()
        run = github_response("repos/MoeGolibrary/moego-mobile/actions/runs/12345")
        with patch.dict(os.environ, {"RELEASE_APPROVAL_EXECUTE": "false"}, clear=False):
            with self.assertRaisesRegex(release.ApprovalError, "live_execution_disabled"):
                release.execute(value, "MoeGolibrary/moego-mobile", 12345, 7, run)


if __name__ == "__main__":
    unittest.main()
