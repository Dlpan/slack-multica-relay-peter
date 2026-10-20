#!/usr/bin/env python3
"""Owner-only Slack ticket actions. Stdlib only; dry-run unless --execute."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import shlex
import sys

from codex_context import Codex, Unavailable, pages, item_text
import urllib.error
import urllib.parse
import urllib.request


class Blocked(Exception):
    def __init__(self, code, details=None):
        super().__init__(code)
        self.details = details


def require(condition, code):
    if not condition:
        raise Blocked(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def text_of(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_of(x) for x in value)
    if isinstance(value, dict):
        if value.get("type") == "text":
            return value.get("text", "")
        separator = "" if value.get("type") == "paragraph" else "\n"
        return separator.join(text_of(x) for x in value.get("content", []))
    return ""


def matches(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and matches(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(matches(a, b) for a, b in zip(actual, expected))
    return actual == expected


def english(value):
    # Semantic English quality is reviewed by the Agent; reject non-Latin prose here.
    if isinstance(value, str):
        require(not re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff]", value), "jira_content_must_be_english")
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in ("id", "accountId", "key", "self"):
                english(item)
    elif isinstance(value, list):
        for item in value:
            english(item)


def adf(text):
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else
        {"type": "paragraph", "content": []} for line in text.split("\n")]}


def validation_details(raw):
    """Retain field IDs and fixed error categories, never server/customer text."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    def category(message):
        if not isinstance(message, str):
            return "rejected"
        text = message.casefold()
        if any(word in text for word in ("atlassian document", "valid document", "adf")):
            return "invalid_document"
        if any(word in text for word in ("required", "must specify", "mandatory")):
            return "required"
        if any(word in text for word in ("cannot be set", "not on the appropriate screen", "not editable")):
            return "not_editable"
        if any(word in text for word in ("option", "allowed value")):
            return "invalid_option"
        return "rejected"
    fields = data.get("errors", {})
    messages = data.get("errorMessages", [])
    system_fields = {"body", "comment", "properties", "resolution", "transition", "status", "summary", "description",
                     "environment", "assignee", "reporter", "priority", "components", "labels", "fixVersions", "versions"}
    safe = {"fields": {key: category(value) for key, value in fields.items()
                        if re.fullmatch(r"customfield_\d+", key) or key in system_fields}} if isinstance(fields, dict) else {"fields": {}}
    safe["categories"] = sorted({category(message) for message in messages}) if isinstance(messages, list) else []
    return safe if safe["fields"] or safe["categories"] else None


class Api:
    def __init__(self, env=None):
        env = dict(os.environ if env is None else env)
        # Explicit owner-local credential source; never load a task's CWD .env.
        if env.get("TICKET_JIRA_ENV_FILE"):
            path = Path(env["TICKET_JIRA_ENV_FILE"])
            require(path.is_absolute() and path.is_file() and path.stat().st_uid == os.getuid(), "invalid_jira_env_file")
            for line in path.read_text().splitlines():
                key, sep, raw = line.partition("=")
                key = key.strip().removeprefix("export ")
                if sep and key in ("JIRA_BASE_URL", "JIRA_LOGIN", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_TOKEN"):
                    values = shlex.split(raw, comments=True)
                    if values and not env.get(key):
                        env[key] = values[0]
        self.slack_token = env.get("SLACK_USER_TOKEN", "")
        self.owner = env.get("RELAY_OWNER_SLACK_USER_ID", "")
        self.team = env.get("SLACK_TEAM_ID", "")
        self.base = env.get("JIRA_BASE_URL", "https://moego.atlassian.net").rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        require(parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment and not parsed.path,
                "invalid_jira_url")
        login = env.get("JIRA_LOGIN") or env.get("JIRA_EMAIL", "")
        token = env.get("JIRA_API_TOKEN") or env.get("JIRA_TOKEN", "")
        self.jira_auth = "Basic " + base64.b64encode(f"{login}:{token}".encode()).decode()
        self.jira_ready = bool(login and token)
        self.jira_account = env.get("TICKET_JIRA_ACCOUNT_ID", "")

    def request(self, url, method, body, authorization):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method,
            headers={"Authorization": authorization, "Content-Type": "application/json", "Accept": "application/json"})
        # Never follow a credential-bearing request to a different origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=25) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            details = None
            try:
                if exc.code == 400 and url.startswith(self.base + "/rest/api/3/"):
                    details = validation_details(exc.read(65536))
            finally:
                exc.close()
            raise Blocked(f"http_{exc.code}", details) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise Blocked("network_or_response_unknown") from None

    def slack(self, method, body):
        require(bool(self.slack_token), "missing_slack_user_token")
        url = "https://slack.com/api/" + method
        if method in ("auth.test", "conversations.replies", "emoji.list"):
            result = self.request(url + "?" + urllib.parse.urlencode(body), "GET", None, "Bearer " + self.slack_token)
        else:
            result = self.request(url, "POST", body, "Bearer " + self.slack_token)
        require(result.get("ok") is True or result.get("error") in ("already_reacted", "no_reaction"),
                "slack_" + str(result.get("error", "unknown")))
        return result

    def jira(self, path, method="GET", body=None):
        require(self.jira_ready, "missing_jira_credentials")
        return self.request(self.base + "/rest/api/3/" + path, method, body, self.jira_auth)


def verified_thread(api, event):
    require(bool(api.owner) and bool(api.team), "missing_owner_or_team")
    require(event.get("teamId") == api.team and event.get("senderUserId") == api.owner
            and not event.get("sourceAppId"), "owner_only")
    for key in ("channelId",):
        require(bool(re.fullmatch(r"[CDG][A-Z0-9]+", event.get(key, ""))), "invalid_event")
    for key in ("messageTs", "threadTs"):
        require(bool(re.fullmatch(r"\d+\.\d+", event.get(key, ""))), "invalid_event")
    actor = api.slack("auth.test", {})
    require(actor.get("user_id") == api.owner and actor.get("team_id") == api.team
            and not actor.get("bot_id"), "slack_actor_mismatch")
    messages, cursor, seen = [], "", set()
    while True:
        page = api.slack("conversations.replies", {"channel": event["channelId"],
            "ts": event["threadTs"], "limit": 100, **({"cursor": cursor} if cursor else {})})
        messages.extend(page.get("messages", []))
        cursor = page.get("response_metadata", {}).get("next_cursor", "")
        require(not page.get("has_more") or bool(cursor), "incomplete_slack_thread")
        if not cursor:
            break
        require(cursor not in seen, "invalid_slack_cursor")
        seen.add(cursor)
    found = [m for m in messages if m.get("ts") == event["messageTs"]]
    require(len(found) == 1, "trigger_message_missing")
    message = found[0]
    require(message.get("user") == api.owner and not message.get("app_id") and not message.get("bot_id")
            and message.get("subtype") not in ("bot_message", "message_changed", "message_deleted"), "owner_only")
    require(message.get("text") == event.get("text"), "trigger_changed")
    require(bool(re.search(r"<@" + re.escape(api.owner) + r"(?:\|[^>]+)?>", message.get("text", ""))), "direct_owner_mention_required")
    return messages


def reaction(api, event, name, remove=False):
    api.slack("reactions.remove" if remove else "reactions.add", {
        "channel": event["channelId"], "timestamp": event["messageTs"], "name": name})


class Journal:
    """Durable write intents plus a process lock, shared by all runs on this Runtime."""
    def __init__(self, directory):
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path / "actions.sqlite", timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS actions (key TEXT PRIMARY KEY, digest TEXT, phase TEXT, receipt TEXT)")
        self.db.commit()

    @contextlib.contextmanager
    def lock(self, key, busy_code="ticket_lock_busy"):
        with (self.path / (fingerprint(key) + ".lock")).open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked(busy_code) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def get(self, key):
        row = self.db.execute("SELECT digest, phase, receipt FROM actions WHERE key=?", (key,)).fetchone()
        return {"digest": row[0], "phase": row[1], "receipt": json.loads(row[2])} if row else None

    def put(self, key, digest, phase, receipt=None):
        self.db.execute("INSERT OR REPLACE INTO actions VALUES(?,?,?,?)", (key, digest, phase, json.dumps(receipt or {})))
        self.db.commit()

    def close(self):
        self.db.close()


def comments(api, issue):
    start = 0
    while True:
        page = api.jira(f"issue/{issue}/comment?startAt={start}&maxResults=100&expand=properties")
        rows = page.get("comments", [])
        yield from rows
        start += len(rows)
        if start >= page.get("total", start):
            break
        require(bool(rows), "incomplete_jira_comments")


def empty(value):
    return value is None or value == "" or value == [] or value == {} or (
        isinstance(value, dict) and value.get("type") == "doc" and not text_of(value).strip())


BUG_FIELDS = {"devResult": "Dev Result", "rootCause": "Root Cause", "solution": "Solution"}


def is_bug(issue):
    return bool(re.search(r"\bbug\b", issue["fields"].get("issuetype", {}).get("name", ""), re.I))


def bug_values(plan):
    values = plan.get("bugFix", {})
    require(isinstance(values, dict) and set(values) <= set(BUG_FIELDS), "invalid_bug_fix_fields")
    return values


def bug_start_preflight(api, issue, plan):
    # Open may expose only Start Dev. Require the known business facts before
    # taking that first step; bind field IDs/options when Bug Fixed becomes live.
    detail = api.jira(f"issue/{issue['id']}?expand=names")
    values = bug_values(plan)
    for semantic, name in BUG_FIELDS.items():
        ids = [field for field, label in detail.get("names", {}).items() if label.casefold() == name.casefold()]
        current = detail["fields"].get(ids[0]) if len(ids) == 1 else None
        value = values.get(semantic, current)
        require(not empty(value), "missing_required_field:" + name)
        if semantic in values and not empty(current):
            same = (isinstance(value, str) and (current.get("value") if isinstance(current, dict) and semantic == "devResult"
                    else text_of(current)) == value) or matches(current, value)
            require(same, "existing_field_would_change")
        if semantic in values and not matches(current, value):
            english(value)


def transition_fields(issue, selected, supplied, required_names=()):
    fields = issue["fields"]
    screen = selected.get("fields", {})
    require(isinstance(supplied, dict), "invalid_fields")
    require(set(supplied) <= set(screen), "field_not_on_transition_screen")
    required_ids = set()
    for name in required_names:
        ids = [field for field, meta in screen.items() if meta.get("name", "").casefold() == name.casefold()]
        require(len(ids) == 1, "bug_field_metadata_missing:" + name)
        required_ids.add(ids[0])
    patch = {}
    for field, value in supplied.items():
        if isinstance(value, str) and screen[field].get("schema", {}).get("custom", "").endswith(":textarea"):
            value = adf(value)
        if not empty(fields.get(field)) and isinstance(value, dict) and value.get("type") == "doc" \
                and text_of(fields[field]) == text_of(value):
            value = fields[field]
        require(empty(fields.get(field)) or matches(fields.get(field), value), "existing_field_would_change")
        if not matches(fields.get(field), value):
            english(value)
            patch[field] = value
    for field, meta in screen.items():
        if meta.get("required") or field in required_ids:
            value = patch.get(field, fields.get(field))
            require(not empty(value), "missing_required_field:" + meta.get("name", field))
            patch[field] = value
        value = patch.get(field)
        options = meta.get("allowedValues", [])
        if value is not None and options:
            for entry in value if isinstance(value, list) else [value]:
                require(any(entry == option or isinstance(entry, dict) and any(
                    k in entry for k in ("id", "value", "name")) and all(
                        str(entry[k]) == str(option.get(k)) for k in ("id", "value", "name") if k in entry)
                    for option in options), "invalid_field_option:" + field)
    return patch


def bug_fixed_fields(issue, selected, plan):
    screen = selected.get("fields", {})
    supplied = {}
    for semantic, value in bug_values(plan).items():
        name = BUG_FIELDS[semantic]
        ids = [field for field, meta in screen.items() if meta.get("name", "").casefold() == name.casefold()]
        require(len(ids) == 1, "bug_field_metadata_missing:" + name)
        field, meta = ids[0], screen[ids[0]]
        if semantic == "devResult" and isinstance(value, str):
            options = [o for o in meta.get("allowedValues", []) if o.get("value", "").casefold() == value.casefold()]
            require(len(options) == 1, "invalid_field_option:" + field)
            value = {"id": options[0]["id"]}
        elif isinstance(value, str) and meta.get("schema", {}).get("custom", "").endswith(":textarea"):
            value = adf(value)
        supplied[field] = value
    return transition_fields(issue, selected, supplied, BUG_FIELDS.values())


def transition_plan(api, issue, plan):
    current = issue["fields"]["status"]
    status = current.get("name", "").casefold()
    if status in ("closed", "resolved"):
        return {"already_complete": True, "status": current}
    available = [t for t in api.jira(f"issue/{issue['id']}/transitions?expand=transitions.fields").get("transitions", [])
                 if t.get("isAvailable") is not False]
    target = plan.get("targetStatus")
    require(target is None or target.casefold() in ("closed", "resolved"), "invalid_target_status")
    if is_bug(issue) and status == "in testing":
        target = "Resolved"
    candidates = [t for t in available if t.get("to", {}).get("name", "").casefold()
                  in ([target.casefold()] if target else ["closed", "resolved"])]
    if plan.get("transitionId"):
        candidates = [t for t in candidates if str(t["id"]) == str(plan["transitionId"])]
    intermediate = False
    if not candidates and is_bug(issue) and status in ("open", "in progress"):
        require(target is None or target.casefold() == "resolved", "bug_workflow_target_must_be_resolved")
        candidates = [t for t in available if t.get("name", "").casefold() == "bug fixed"
                      and t.get("to", {}).get("name", "").casefold() == "in testing"]
        if not candidates and status == "open":
            candidates = [t for t in available if t.get("name", "").casefold() == "start dev"
                          and t.get("to", {}).get("name", "").casefold() == "in progress"]
            if len(candidates) == 1:
                bug_start_preflight(api, issue, plan)
        intermediate = True
    require(len(candidates) == 1, "terminal_transition_missing_or_ambiguous")
    selected = candidates[0]
    if intermediate and selected.get("name", "").casefold() == "bug fixed":
        patch = bug_fixed_fields(issue, selected, plan)
    else:
        patch = transition_fields(issue, selected, {} if intermediate else plan.get("fields", {}))
    return {"transition": {"id": selected["id"]}, "fields": patch, "target": selected["to"],
            "intermediate": intermediate, **({"pendingFinalState": "Resolved", "conditional": True} if intermediate else {})}


def validate_request(plan, event):
    require(bool(re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", plan.get("issue", ""))), "invalid_issue_key")
    actions = plan.get("actions")
    require(isinstance(actions, list) and bool(actions) and len(actions) == len(set(actions))
            and set(actions) <= {"reply", "close"}, "invalid_actions")
    require(plan.get("requestConfirmed") is True, "explicit_request_required")
    require(plan.get("requestText") == event["text"], "request_text_mismatch")
    return [action for action in ("reply", "close") if action in actions]


def verify_codex_evidence(item, issue):
    client = None
    try:
        client = Codex()
        turns = list(pages(client, "thread/turns/list", {"threadId": item["threadId"],
            "limit": 50, "itemsView": "full", "sortDirection": "asc"}))
        keys = [issue] if isinstance(issue, str) else issue
        require(any(any(re.search(r"(?<![A-Z0-9_])" + re.escape(key) + r"(?![A-Z0-9_])", item_text(record), re.I) for key in keys)
                    for turn in turns for record in turn.get("items", [])
                    if record.get("type") in ("userMessage", "agentMessage")), "codex_ticket_mismatch")
        require(any(turn["id"] == item.get("turnId") and record.get("id") == item.get("itemId")
                    and item["quote"] in item_text(record) for turn in turns for record in turn.get("items", [])),
                "codex_evidence_mismatch")
    except (Unavailable, OSError):
        raise Blocked("codex_evidence_unavailable") from None
    finally:
        if client:
            client.close()


def validate_reply(plan, messages, issue_keys=None):
    roots = plan.get("rootCause", {})
    require(roots.get("confirmed") is True and bool(roots.get("summary", "").strip()), "root_cause_not_confirmed")
    evidence = roots.get("evidence", [])
    require(bool(evidence), "root_cause_evidence_required")
    for item in evidence:
        require(item.get("source") in ("slack", "codex") and bool(item.get("quote", "").strip()), "invalid_evidence")
        if item["source"] == "slack":
            require(any(m.get("ts") == item.get("messageTs") and item["quote"] in m.get("text", "")
                        for m in messages), "slack_evidence_mismatch")
        else:
            require(all(item.get(k) for k in ("threadId", "turnId", "itemId")), "codex_evidence_reference_required")
            verify_codex_evidence(item, issue_keys or plan["issue"])
    body = plan.get("comment", "")
    require(isinstance(body, str) and body.startswith("#intercom\n") and bool(body[len("#intercom\n"):].strip()), "intercom_comment_required")
    english(body)


def post_recorded(api, journal, key, digest, path, body, intent=None):
    journal.put(key, digest, "writing", intent)
    try:
        return api.jira(path, "POST", body)
    except Blocked as exc:
        if re.fullmatch(r"http_4\d\d", str(exc)) and str(exc) not in ("http_408", "http_429"):
            journal.put(key, digest, "rejected")
        raise


def close_ticket(api, event, plan, journal, key, digest, execute, issue_id):
    prior = journal.get(key)
    progress = prior["receipt"] if prior and prior["phase"] == "workflow" else {"steps": []}
    for _ in range(4):
        verified_thread(api, event)
        fresh = api.jira("issue/" + issue_id)
        require(fresh.get("id") == issue_id, "jira_issue_mismatch")
        if execute and progress.get("active"):
            step_key = progress["active"]
            step = journal.get(step_key)
            require(bool(step), "invalid_workflow_state")
            intent = step["receipt"]
            if step["phase"] != "rejected":
                require(fresh["fields"]["status"]["id"] == intent["target"]["id"], "ambiguous_transition_write")
                require(all(matches(fresh["fields"].get(f), v) for f, v in intent.get("fields", {}).items()),
                        "transition_reconciliation_failed")
                checkpoint = {"transitionId": intent["transitionId"], "status": intent["target"]["name"], "verified": True}
                journal.put(step_key, step["digest"], "done", intent)
                progress["steps"].append(checkpoint)
            progress.pop("active")
            journal.put(key, digest, "workflow", progress)
        close = transition_plan(api, fresh, plan)
        if not execute:
            return close
        if close.get("already_complete"):
            if prior and prior["phase"] == "writing":
                intent = prior["receipt"]
                require(fresh["fields"]["status"]["id"] == intent.get("target", {}).get("id")
                        and all(matches(fresh["fields"].get(f), v) for f, v in intent.get("fields", {}).items()),
                        "transition_reconciliation_failed")
            return {"status": fresh["fields"]["status"]["name"], "verified": True,
                    "already_complete": True, "steps": progress["steps"]}
        require(not prior or prior["phase"] != "writing", "ambiguous_transition_write")
        staged = close["intermediate"] or bool(progress["steps"])
        intent = {"target": close["target"], "fields": close["fields"], "transitionId": close["transition"]["id"]}
        step_key = key + ":step:" + fingerprint([fresh["fields"]["status"]["id"], close["transition"]["id"]]) if staged else key
        if staged:
            # The parent points at the pending step before its POST. A retry must
            # reconcile that step before discovering or submitting the next one.
            progress["active"] = step_key
            journal.put(key, digest, "workflow", progress)
        post_recorded(api, journal, step_key, digest, f"issue/{fresh['id']}/transitions",
                      {"transition": close["transition"], "fields": close["fields"]}, intent)
        saved = api.jira("issue/" + fresh["id"])
        require(saved["fields"]["status"]["id"] == close["target"]["id"], "transition_verification_failed")
        require(all(matches(saved["fields"].get(f), v) for f, v in close["fields"].items()), "field_verification_failed")
        checkpoint = {"transitionId": close["transition"]["id"], "status": saved["fields"]["status"]["name"], "verified": True}
        if staged:
            journal.put(step_key, digest, "done", intent)
            progress["steps"].append(checkpoint)
            progress.pop("active")
            journal.put(key, digest, "workflow", progress)
        if not close["intermediate"]:
            return {"status": saved["fields"]["status"]["name"], "verified": True, "steps": progress["steps"]}
    raise Blocked("workflow_step_limit")


def resolve_issue(api, plan, messages):
    source = plan.get("sourceIssue", plan["issue"])
    require(isinstance(source, str) and bool(re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", source)), "invalid_issue_key")
    require(any(re.search(r"(?<![A-Z0-9_])" + re.escape(source) + r"(?![A-Z0-9_])", m.get("text", ""), re.I)
                for m in messages), "ticket_not_in_slack_thread")
    issue = api.jira("issue/" + plan["issue"])
    require(bool(re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", issue.get("key", ""))) and str(issue.get("id", "")).isdigit(),
            "jira_issue_mismatch")
    if source != plan["issue"]:
        original = api.jira("issue/" + source)
        require(original.get("id") == issue["id"] and original.get("key") == issue["key"], "jira_alias_mismatch")
    return issue


def execute_plan(api, event, plan, journal, execute=False):
    messages = verified_thread(api, event)
    actions = validate_request(plan, event)
    actor = api.jira("myself")["accountId"]
    require(bool(api.jira_account) and actor == api.jira_account, "jira_actor_mismatch")
    issue = resolve_issue(api, plan, messages)
    operation = fingerprint([api.base, event["teamId"], event["channelId"], event["messageTs"], issue["id"]])
    result = {"issue": issue["key"], "issueId": issue["id"], "dry_run": not execute, "completed": [], "receipts": {}, "errors": {}}
    with journal.lock([api.base, issue["id"]]):
        for action in actions:
            key = operation + ":" + action
            try:
                messages = verified_thread(api, event)
                content = plan.get("comment") if action == "reply" else {k: plan.get(k) for k in ("targetStatus", "transitionId", "fields")}
                if action == "close" and "bugFix" in plan:
                    content["bugFix"] = plan["bugFix"]
                digest = fingerprint(content)
                previous = journal.get(key)
                changeable_workflow = previous and previous["phase"] == "workflow" and (not previous["receipt"].get("active")
                    or (journal.get(previous["receipt"]["active"]) or {}).get("phase") == "rejected")
                require(not previous or previous["phase"] == "rejected" or changeable_workflow or previous["digest"] == digest,
                        "operation_content_changed")
                if previous and previous["phase"] == "done":
                    result["completed"].append(action)
                    result["receipts"][action] = previous["receipt"]
                    continue
                if action == "reply":
                    validate_reply(plan, messages, {plan["issue"], issue["key"], plan.get("sourceIssue", plan["issue"])})
                    if not execute:
                        result["receipts"][action] = {"ready": True, "comment": plan["comment"]}
                        continue
                    existing = [c for c in comments(api, issue["id"]) if c.get("author", {}).get("accountId") == actor
                                and text_of(c.get("body")) == plan["comment"]]
                    if existing:
                        receipt = {"commentId": existing[-1]["id"], "verified": True, "recovered": True}
                    else:
                        require(not previous or previous["phase"] != "writing", "ambiguous_comment_write")
                        created = post_recorded(api, journal, key, digest, f"issue/{issue['id']}/comment",
                            {"body": adf(plan["comment"]), "properties": [{"key": "slack-ticket-action", "value": operation}]})
                        saved = api.jira(f"issue/{issue['id']}/comment/{created['id']}")
                        require(saved.get("author", {}).get("accountId") == actor and text_of(saved.get("body")) == plan["comment"],
                                "comment_verification_failed")
                        receipt = {"commentId": saved["id"], "verified": True}
                else:
                    receipt = close_ticket(api, event, plan, journal, key, digest, execute, issue["id"])
                    if not execute:
                        result["receipts"][action] = receipt
                        continue
                journal.put(key, digest, "done", receipt)
                result["completed"].append(action)
                result["receipts"][action] = receipt
            except Blocked as exc:
                result["errors"][action] = str(exc)
                if exc.details:
                    result.setdefault("validation", {})[action] = exc.details
                if action == "close":
                    state = journal.get(key)
                    if state and state["phase"] == "workflow":
                        result["receipts"][action] = {"complete": False, "steps": state["receipt"].get("steps", [])}
                # Unknown write outcomes require reconciliation before another mutation.
                if str(exc).startswith(("network_", "ambiguous_", "http_5")):
                    break
    if result["errors"]:
        result["error"] = "ticket_actions_incomplete"
    return result


def explain(code):
    messages = {
        "root_cause_not_confirmed": "尚未定位到有证据支持的具体原因，未发送 #intercom 评论",
        "root_cause_evidence_required": "缺少支持具体原因的调查证据，未发送评论",
        "terminal_transition_missing_or_ambiguous": "Jira 当前没有唯一明确的终态或已授权 Bug 工作流动作，需要核对具体流转；未要求管理员新增直接关闭",
        "jira_content_must_be_english": "拟写入 Jira 的内容包含非英文文本，需要先改为英文",
        "existing_field_would_change": "拟补充的字段与已有内容冲突，需要核实后再处理",
        "ambiguous_comment_write": "上次评论写入结果不明，需要先核对 Jira，未重复提交",
        "ambiguous_transition_write": "上次状态流转结果不明，需要先核对 Jira，未重复提交",
        "network_or_response_unknown": "请求超时或响应异常，需要先核对 Jira 是否已执行，未盲目重试",
        "http_400": "Jira 拒绝写入，可能存在额外的必填字段或工作流校验，需要检查 Jira 表单",
        "http_401": "Jira 身份认证失败，需要更新授权配置",
        "http_403": "当前 Jira 身份没有执行权限，需要检查项目权限或工作流限制",
        "ticket_lock_busy": "另一个任务正在处理同一 ticket，请等待该任务结束后再重试",
        "jira_alias_mismatch": "Slack 中的旧编号与目标编号未解析到同一 Jira issue，未执行",
    }
    if code.startswith("missing_required_field:"):
        return "Jira 必填字段 " + code.split(":", 1)[1] + " 缺少可确认的值，请补充该字段信息"
    return messages.get(code, "校验或执行受阻（" + code + "），需要核实对应信息后继续")


def outcome_key(api, event, plan):
    return fingerprint(["outcome", api.base, event, plan.get("issue")])


def failure_key(event, plan):
    return fingerprint(["failure", event, plan.get("issue")])


def failure_text(result):
    completed = [{"reply": "已发送 #intercom 评论", "close": "已完成关闭"}[action]
                 for action in result.get("completed", [])]
    steps = result.get("receipts", {}).get("close", {}).get("steps", [])
    if steps and "close" not in result.get("completed", []):
        completed.append("已流转至 " + steps[-1]["status"] + "（关闭尚未完成）")
    reasons = "；".join(explain(code) for code in result.get("errors", {}).values())
    for action, detail in result.get("validation", {}).items():
        fields = ", ".join(field + ": " + code for field, code in detail.get("fields", {}).items())
        if fields:
            reasons += "；" + action + " 字段校验：" + fields
    current = result.get("receipts", {}).get("close", {}).get("currentStatus")
    if current:
        reasons = "工单当前状态为 " + current + "；" + reasons
    return ("这次工单操作未全部完成（" + result["issue"] + "）。已完成：" + ("；".join(completed) or "无") + "；原因：" + reasons +
            "。已完成的步骤会保留，后续重试会先核对实际状态。")


def correct_failure_notice(api, event, plan, journal):
    key = failure_key(event, plan)
    previous = journal.get(key)
    if not previous or previous["phase"] == "corrected":
        return
    receipt = previous["receipt"]
    # A timed-out post is reconciled against its exact persisted text and owner.
    # Legacy notices have no persisted text/ts and cannot be safely edited here.
    if not receipt.get("messageTs") and receipt.get("text"):
        found = [m for m in verified_thread(api, event) if m.get("user") == api.owner
                 and m.get("text") == receipt["text"] and m.get("ts", "") >= event["messageTs"]]
        require(len(found) <= 1, "ambiguous_failure_notice")
        if found:
            receipt["messageTs"] = found[0]["ts"]
        require(bool(found), "failure_notice_outcome_unknown")
    if receipt.get("messageTs"):
        api.slack("chat.update", {"channel": event["channelId"], "ts": receipt["messageTs"],
            "text": "本次工单操作已在后续重试中全部完成（" + plan["issue"] + "）。此前失败提示对应中途状态，现已更正。"})
    journal.put(key, "", "corrected", receipt)


def finalize_failure(api, event, plan, journal):
    """Publish the final blocker only; never submit Jira comments/transitions."""
    with journal.lock(["event", api.base, event.get("teamId"), event.get("channelId"), event.get("messageTs")],
                      "same_message_running"):
        messages = verified_thread(api, event)
        validate_request(plan, event)
        saved = journal.get(outcome_key(api, event, plan))
        require(saved and saved["digest"] == fingerprint(plan), "current_execution_result_required")
        result = saved["receipt"]
        require("error" in result, "failure_finalization_requires_error")
        require(api.jira("myself").get("accountId") == api.jira_account and bool(api.jira_account), "jira_actor_mismatch")
        issue = resolve_issue(api, plan, messages)
        require(issue["id"] == result.get("issueId", issue["id"]), "jira_issue_mismatch")
        with journal.lock([api.base, issue["id"]]):
            fresh = api.jira("issue/" + issue["id"])
            require(fresh.get("id") == issue["id"], "jira_issue_mismatch")
            issue = fresh
            if "close" in result.get("errors", {}):
                require(issue["fields"]["status"]["name"].casefold() not in ("closed", "resolved"),
                        "outcome_needs_reconciliation")
                result.setdefault("receipts", {}).setdefault("close", {})["currentStatus"] = issue["fields"]["status"]["name"]
            if "reply" in result.get("errors", {}):
                require(not any(c.get("author", {}).get("accountId") == api.jira_account
                                and text_of(c.get("body")) == plan.get("comment")
                                for c in comments(api, issue["id"])), "outcome_needs_reconciliation")
            key = failure_key(event, plan)
            previous = journal.get(key)
            if previous and previous["phase"] != "corrected":
                require(previous["phase"] == "done", "failure_notice_outcome_unknown")
                receipt = previous["receipt"]
                message = failure_text(result)
                if receipt.get("messageTs") and receipt.get("text") != message:
                    api.slack("chat.update", {"channel": event["channelId"], "ts": receipt["messageTs"], "text": message})
                    journal.put(key, "", "done", {**receipt, "text": message})
            else:
                message = failure_text(result)
                journal.put(key, "", "writing", {"text": message})
                posted = api.slack("chat.postMessage", {"channel": event["channelId"], "thread_ts": event["threadTs"],
                    "text": message, "unfurl_links": False, "unfurl_media": False})
                journal.put(key, "", "done", {"text": message, "messageTs": posted["ts"]})
            reaction(api, event, "ack", remove=True)
            result["notification_pending"] = False
            journal.put(outcome_key(api, event, plan), fingerprint(plan), "done", result)
            return result


def _run(api, event, plan, journal, execute=False):
    if not execute:
        return execute_plan(api, event, plan, journal, False)
    verified_thread(api, event)
    require(plan.get("requestConfirmed") is True and plan.get("requestText") == event.get("text"), "explicit_request_required")
    # An admitted, semantically confirmed request gets ack before checking root cause/fields.
    reaction(api, event, "ack")
    try:
        result = execute_plan(api, event, plan, journal, True)
    except Blocked as exc:
        result = {"issue": plan.get("issue"), "error": str(exc), "completed": [], "errors": {"preflight": str(exc)}}
        if exc.details:
            result["validation"] = {"preflight": exc.details}
    result["notification_pending"] = "error" in result
    journal.put(outcome_key(api, event, plan), fingerprint(plan), "done", result)
    if "error" not in result:
        correct_failure_notice(api, event, plan, journal)
        reaction(api, event, "ack", remove=True)
        reaction(api, event, "white_check_mark")
    return result


def run(api, event, plan, journal, execute=False):
    if not execute:
        return _run(api, event, plan, journal, False)
    key = ["event", api.base, event.get("teamId"), event.get("channelId"), event.get("messageTs")]
    with journal.lock(key, "same_message_running"):
        return _run(api, event, plan, journal, True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "run", "finalize-failure"])
    parser.add_argument("--event", required=True, help="Path to original relay eventPayload JSON")
    parser.add_argument("--plan", help="Path to Agent-reviewed plan JSON")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    api = Api()
    event = json.loads(Path(args.event).read_text())
    if "eventPayload" in event:
        event = event["eventPayload"]
    if args.command == "inspect":
        print(json.dumps({"messages": verified_thread(api, event)}, ensure_ascii=False))
        return
    require(bool(args.plan), "plan_required")
    directory = os.environ.get("TICKET_STATE_DIR")
    require(bool(directory) and Path(directory).is_absolute(), "missing_persistent_state_directory")
    journal = Journal(directory)
    try:
        plan = json.loads(Path(args.plan).read_text())
        if args.command == "finalize-failure":
            require(args.execute, "execute_required")
            result = finalize_failure(api, event, plan, journal)
        else:
            result = run(api, event, plan, journal, args.execute)
        print(json.dumps(result, ensure_ascii=False))
        if "error" in result:
            sys.exit(4)
    finally:
        journal.close()


if __name__ == "__main__":
    try:
        main()
    except Blocked as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(4)
