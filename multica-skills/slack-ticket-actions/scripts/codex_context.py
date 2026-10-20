#!/usr/bin/env python3
"""Read local Codex ticket evidence without resuming or sending messages to chats."""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading


class Unavailable(Exception):
    pass


class Codex:
    ALLOWED = {"initialize", "thread/list", "thread/read", "thread/turns/list"}

    def __init__(self):
        env = os.environ.copy()
        # Multica may isolate CODEX_HOME. Read the owner's configured history store.
        env["CODEX_HOME"] = os.environ.get("TICKET_CODEX_HOME", str(Path.home() / ".codex"))
        self.process = subprocess.Popen([os.environ.get("TICKET_CODEX_BIN", "codex"), "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        self.messages = queue.Queue()
        self.index = 0
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "slack_ticket_context", "version": "1.0.0"}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _reader(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                pass
        self.messages.put(None)

    def send(self, body):
        self.process.stdin.write(json.dumps(body) + "\n")
        self.process.stdin.flush()

    def call(self, method, params):
        if method not in self.ALLOWED:
            raise Unavailable("read_only_method_required")
        self.index += 1
        id = self.index
        self.send({"id": id, "method": method, "params": params})
        while True:
            try:
                message = self.messages.get(timeout=30)
            except queue.Empty:
                raise Unavailable("codex_read_timeout") from None
            if message is None:
                raise Unavailable("codex_process_ended")
            if message.get("id") == id:
                if "error" in message:
                    raise Unavailable("codex_rpc_" + str(message["error"].get("code", "unknown")))
                return message["result"]

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def pages(client, method, params, limit=200):
    cursor, seen = None, set()
    for _ in range(limit):
        page = client.call(method, {**params, **({"cursor": cursor} if cursor else {})})
        yield from page.get("data", [])
        cursor = page.get("nextCursor")
        if not cursor:
            return
        if cursor in seen:
            raise Unavailable("codex_invalid_cursor")
        seen.add(cursor)
    raise Unavailable("codex_coverage_limit")


def item_text(item):
    if item.get("type") == "agentMessage":
        return item.get("text", "")
    if item.get("type") == "userMessage":
        return "\n".join(x.get("text", "") for x in item.get("content", []) if x.get("type") == "text")
    if item.get("type") == "commandExecution":
        # Tool results substantiate execution; never execute the recorded command.
        return item.get("aggregatedOutput", "") or str(item.get("output", ""))
    return ""


def search(client, issue, projects=None, max_threads=1000):
    match = re.compile(r"(?<![A-Z0-9_])" + re.escape(issue) + r"(?![A-Z0-9_])", re.I)
    hits, limitations, scanned = [], [], 0
    sources = ["cli", "vscode", "exec", "appServer", "unknown"]
    for archived in (False, True):
        params = {"limit": 100, "archived": archived, "sourceKinds": sources, "sortKey": "updated_at"}
        if projects:
            params["cwd"] = projects
        for thread in pages(client, "thread/list", params):
            scanned += 1
            if scanned > max_threads:
                limitations.append("thread_scan_limit")
                return {"threads": hits, "scanned": scanned - 1, "complete": False, "limitations": limitations}
            try:
                turns = list(pages(client, "thread/turns/list", {"threadId": thread["id"],
                    "limit": 50, "itemsView": "full", "sortDirection": "asc"}))
            except Unavailable as exc:
                limitations.append({"threadId": thread["id"], "reason": str(exc)})
                continue
            records = [{"turnId": turn["id"], "status": turn.get("status"), "itemId": item.get("id"),
                        "type": item.get("type"), "text": item_text(item)}
                       for turn in turns for item in turn.get("items", []) if item_text(item)]
            # Search full user/assistant messages, never title-only or injected skill inventories.
            relevant = [i for i, record in enumerate(records) if record["type"] in ("userMessage", "agentMessage")
                        and match.search(record["text"]) and not record["text"].startswith(("# AGENTS.md instructions", "<environment_context>"))]
            if relevant:
                hits.append({"threadId": thread["id"], "cwd": thread.get("cwd"), "name": thread.get("name"),
                    "archived": archived, "matchedItemIds": [records[i]["itemId"] for i in relevant],
                    "records": records})
    return {"threads": hits, "scanned": scanned, "complete": not limitations, "limitations": limitations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--project", action="append", help="Exact project/worktree cwd; repeat for all related checkouts")
    parser.add_argument("--output", required=True, help="Local evidence JSON path; never post it to Jira or Slack")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", args.issue):
        raise Unavailable("invalid_issue_key")
    client = Codex()
    try:
        result = search(client, args.issue, args.project)
        destination = Path(args.output)
        destination.write_text(json.dumps(result, ensure_ascii=False))
        destination.chmod(0o600)
        print(json.dumps({"matches": len(result["threads"]), "scanned": result["scanned"],
                          "complete": result["complete"], "limitations": result["limitations"], "output": str(destination)}))
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except (Unavailable, OSError) as exc:
        print(json.dumps({"complete": False, "error": str(exc) if isinstance(exc, Unavailable) else "codex_unavailable"}))
        sys.exit(4)
