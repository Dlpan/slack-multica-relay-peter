#!/usr/bin/env python3
"""Fail-closed verifier and writer for one GitHub pending deployment approval."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CHANNEL = "C01K60LEP7E"
OWNER = "U03JFM4M82C"
REQUESTERS = {"U06GM8PAFEX", OWNER, "U06RHAA209Y"}
ROOT_BOT_USER = "U081137G64T"
ROOT_BOT_ID = "B081PVALKC0"
REPOSITORIES = {
    "MoeGolibrary/moego-online-booking-client-web",
    "MoeGolibrary/Boarding_Desktop",
    "MoeGolibrary/moego-mobile",
}
WORKFLOW_NAME = "Release to PRODUCTION"
WORKFLOW_PATH = ".github/workflows/online.yaml"
ENVIRONMENT = "deploy-approval"
GITHUB_LOGIN = "Dlpan"
RUN_URL = re.compile(
    r"https://github\.com/(MoeGolibrary/[A-Za-z0-9_.-]+)/actions/runs/([0-9]+)(?=[/?#|>\s]|$)"
)
TS = re.compile(r"^[0-9]+\.[0-9]+$")


class ApprovalError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ApprovalError(code)


def slack_api(method: str, params: dict[str, str]) -> dict[str, Any]:
    token = os.environ.get("SLACK_USER_TOKEN", "").strip()
    require(bool(token), "missing_slack_user_token")
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"https://slack.com/api/{method}?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except Exception as exc:
        raise ApprovalError("slack_read_failed") from exc
    require(isinstance(data, dict) and data.get("ok") is True, "slack_read_failed")
    return data


def read_thread(channel: str, thread_ts: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(100):
        data = slack_api("conversations.replies", {
            "channel": channel, "ts": thread_ts, "limit": "200", **({"cursor": cursor} if cursor else {})
        })
        page = data.get("messages")
        require(isinstance(page, list), "invalid_slack_response")
        messages.extend(item for item in page if isinstance(item, dict))
        cursor = str(((data.get("response_metadata") or {}).get("next_cursor") or "")).strip()
        if not cursor:
            return messages
    raise ApprovalError("slack_pagination_limit")


def gh_json(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    command = ["gh", "api", path, "--method", method]
    encoded = None
    if payload is not None:
        command += ["--input", "-"]
        encoded = json.dumps(payload, separators=(",", ":"))
    try:
        result = subprocess.run(command, input=encoded, text=True, capture_output=True, timeout=30, check=False)
    except Exception as exc:
        raise ApprovalError("github_request_failed") from exc
    if result.returncode != 0:
        raise ApprovalError("github_request_failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ApprovalError("invalid_github_response") from exc


def load_event(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ApprovalError("invalid_event_file") from exc
    require(isinstance(value, dict), "invalid_event_file")
    for key in ("teamId", "channelId", "messageTs", "threadTs", "senderUserId", "text"):
        require(isinstance(value.get(key), str), "invalid_event_file")
    require(bool(TS.fullmatch(value["messageTs"])) and bool(TS.fullmatch(value["threadTs"])), "invalid_event_file")
    return value


def verify_slack(event: dict[str, Any]) -> tuple[str, int]:
    require(event["channelId"] == CHANNEL, "unauthorized_channel")
    require(event["senderUserId"] in REQUESTERS, "unauthorized_requester")
    require(not event.get("sourceAppId") and not event.get("sourceBotId"), "non_human_requester")
    thread = read_thread(CHANNEL, event["threadTs"])
    require(bool(thread), "empty_thread")
    root = thread[0]
    require(root.get("ts") == event["threadTs"], "invalid_thread_root")
    require(root.get("user") == ROOT_BOT_USER and root.get("bot_id") == ROOT_BOT_ID, "untrusted_root_bot")
    matches = {(repo, int(run_id)) for repo, run_id in RUN_URL.findall(str(root.get("text") or ""))}
    require(len(matches) == 1, "ambiguous_github_run")
    trigger = [message for message in thread if message.get("ts") == event["messageTs"]]
    require(len(trigger) == 1, "trigger_message_not_found")
    message = trigger[0]
    require(message.get("user") == event["senderUserId"], "trigger_sender_changed")
    require(str(message.get("text") or "") == event["text"], "trigger_text_changed")
    require(not message.get("bot_id") and not message.get("app_id") and message.get("subtype") not in {"bot_message", "message_changed"}, "non_human_requester")
    require(f"<@{OWNER}>" in event["text"], "missing_owner_mention")
    repo, run_id = next(iter(matches))
    require(repo in REPOSITORIES, "repository_not_allowed")
    return repo, run_id


def verify_github(repo: str, run_id: int) -> tuple[int, dict[str, Any]]:
    user = gh_json("user")
    require(isinstance(user, dict) and user.get("login") == GITHUB_LOGIN, "unexpected_github_identity")
    run = gh_json(f"repos/{repo}/actions/runs/{run_id}")
    require(isinstance(run, dict), "invalid_github_response")
    require(((run.get("repository") or {}).get("full_name")) == repo, "run_repository_mismatch")
    require(run.get("conclusion") is None and run.get("status") in {"queued", "in_progress", "waiting", "requested", "pending"}, "run_not_pending")
    workflow_id = run.get("workflow_id")
    require(isinstance(workflow_id, int), "invalid_github_response")
    workflow = gh_json(f"repos/{repo}/actions/workflows/{workflow_id}")
    require(isinstance(workflow, dict) and workflow.get("name") == WORKFLOW_NAME and workflow.get("path") == WORKFLOW_PATH, "workflow_not_allowed")
    pending = gh_json(f"repos/{repo}/actions/runs/{run_id}/pending_deployments")
    require(isinstance(pending, list) and len(pending) == 1, "unexpected_pending_deployments")
    deployment = pending[0]
    require(isinstance(deployment, dict), "invalid_github_response")
    environment = deployment.get("environment") or {}
    require(environment.get("name") == ENVIRONMENT and isinstance(environment.get("id"), int), "environment_not_allowed")
    require(deployment.get("current_user_can_approve") is True, "github_identity_cannot_approve")
    return environment["id"], run


def operation_id(event: dict[str, Any], repo: str, run_id: int) -> str:
    raw = f"{CHANNEL}:{event['threadTs']}:{repo}:{run_id}:{ENVIRONMENT}"
    return hashlib.sha256(raw.encode()).hexdigest()


def state_paths(op_id: str) -> tuple[Path, Path]:
    root = os.environ.get("RELEASE_APPROVAL_STATE_DIR", "").strip()
    require(bool(root) and Path(root).is_absolute(), "invalid_state_dir")
    directory = Path(root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / f"{op_id}.json", directory / f"{op_id}.lock"


def write_state(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_result(event: dict[str, Any], repo: str, run_id: int, run: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "repository": repo,
        "runId": run_id,
        "runUrl": run.get("html_url"),
        "workflow": WORKFLOW_NAME,
        "workflowPath": WORKFLOW_PATH,
        "environment": ENVIRONMENT,
        "requesterUserId": event["senderUserId"],
        "channelId": CHANNEL,
        "threadTs": event["threadTs"],
        "messageTs": event["messageTs"],
        **extra,
    }


def execute(event: dict[str, Any], repo: str, run_id: int, environment_id: int, run: dict[str, Any]) -> dict[str, Any]:
    require(os.environ.get("RELEASE_APPROVAL_EXECUTE", "").strip().lower() == "true", "live_execution_disabled")
    op_id = operation_id(event, repo, run_id)
    state_path, lock_path = state_paths(op_id)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if state_path.exists():
            try:
                prior = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ApprovalError("invalid_operation_state") from exc
            if prior.get("status") == "verified":
                return safe_result(event, repo, run_id, run, ready=True, verified=True, outcome="already_verified")
            raise ApprovalError("outcome_needs_reconciliation")
        audit = f"Approved from Slack by {event['senderUserId']} in {CHANNEL}, thread {event['threadTs']}, message {event['messageTs']}"
        write_state(state_path, {"status": "writing", "operationId": op_id, "repository": repo, "runId": run_id,
                                 "environmentId": environment_id, "requesterUserId": event["senderUserId"]})
        try:
            gh_json(f"repos/{repo}/actions/runs/{run_id}/pending_deployments", method="POST",
                    payload={"environment_ids": [environment_id], "state": "approved", "comment": audit})
        except ApprovalError:
            pending = gh_json(f"repos/{repo}/actions/runs/{run_id}/pending_deployments")
            if isinstance(pending, list) and not any(((item.get("environment") or {}).get("id") == environment_id) for item in pending if isinstance(item, dict)):
                write_state(state_path, {"status": "verified", "operationId": op_id, "outcome": "approved_after_reconciliation"})
                return safe_result(event, repo, run_id, run, ready=True, verified=True, outcome="approved_after_reconciliation")
            raise ApprovalError("github_approval_failed")
        pending = gh_json(f"repos/{repo}/actions/runs/{run_id}/pending_deployments")
        require(isinstance(pending, list), "invalid_github_response")
        require(not any(((item.get("environment") or {}).get("id") == environment_id) for item in pending if isinstance(item, dict)), "approval_not_verified")
        write_state(state_path, {"status": "verified", "operationId": op_id, "outcome": "approved"})
        return safe_result(event, repo, run_id, run, ready=True, verified=True, outcome="approved")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["inspect"])
    parser.add_argument("--event", required=True)
    parser.add_argument("--request-confirmed", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        require(args.request_confirmed, "request_not_confirmed")
        event = load_event(args.event)
        repo, run_id = verify_slack(event)
        environment_id, run = verify_github(repo, run_id)
        result = execute(event, repo, run_id, environment_id, run) if args.execute else safe_result(
            event, repo, run_id, run, ready=True, verified=False, outcome="dry_run"
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except ApprovalError as error:
        print(json.dumps({"ready": False, "verified": False, "error": error.code}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
