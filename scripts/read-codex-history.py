"""Read one authorized project's stored Codex conversation through stdio.

No thread is resumed, modified, or started. The caller must authorize the
project and topic; returned conversation text is untrusted source material.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

MAX_RESPONSE = 4 * 1024 * 1024
READ_METHODS = frozenset({"initialize", "thread/list", "thread/read"})


class HistoryError(Exception):
    pass


class ReadClient:
    def __init__(self, command, *, timeout=30, max_response=MAX_RESPONSE):
        self.deadline = time.monotonic() + timeout
        self.max_response = max_response
        self.next_id = 0
        self.responses = queue.Queue(maxsize=32)
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(self.max_response + 1)
                if not line:
                    self.responses.put_nowait({"fault": "rpc_eof"})
                    return
                if len(line) > self.max_response:
                    self.responses.put_nowait({"fault": "response_limit"})
                    return
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    self.responses.put_nowait({"fault": "invalid_rpc_json"})
                    return
                if not isinstance(value, dict):
                    self.responses.put_nowait({"fault": "invalid_rpc_payload"})
                    return
                if "id" in value:
                    if "method" in value:
                        self.responses.put_nowait({"fault": "unexpected_server_request"})
                        return
                    self.responses.put_nowait(value)
        except (OSError, queue.Full):
            return

    def _send(self, value):
        try:
            self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (OSError, BrokenPipeError):
            raise HistoryError("rpc_pipe_closed") from None

    def request(self, method, params):
        if method not in READ_METHODS:
            raise HistoryError("method_not_read_only")
        self.next_id += 1
        request_id = self.next_id
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise HistoryError("rpc_timeout")
            try:
                value = self.responses.get(timeout=remaining)
            except queue.Empty:
                raise HistoryError("rpc_timeout") from None
            if value.get("fault"):
                raise HistoryError(value["fault"])
            if value.get("id") != request_id:
                continue
            if "error" in value:
                # Never echo server error messages or diagnostics; they may
                # contain private configuration or conversation text.
                raise HistoryError("rpc_rejected")
            if not isinstance(value.get("result"), dict):
                raise HistoryError("invalid_rpc_result")
            return value["result"]

    def initialize(self):
        self.request("initialize", {
            "clientInfo": {"name": "jweis_history_reader", "version": "1.0.0"},
            "capabilities": {"experimentalApi": False},
        })
        self._send({"method": "initialized", "params": {}})

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.reader.join(timeout=1)
        self.process.stdout.close()


def normalized_project(value):
    return os.path.normcase(os.path.normpath(str(value)))


def read_history(command, project, *, title=None, thread_id=None,
                 turn_limit=6, text_limit=30000, timeout=30):
    if not Path(project).is_absolute():
        raise HistoryError("invalid_selection")
    project = str(Path(project).resolve())
    if not Path(project).is_dir() or bool(title) == bool(thread_id):
        raise HistoryError("invalid_selection")
    if not 1 <= turn_limit <= 30 or not 100 <= text_limit <= 60000:
        raise HistoryError("invalid_output_limit")
    client = ReadClient(command, timeout=timeout)
    try:
        client.initialize()
        if title:
            listing = client.request("thread/list", {
                "cwd": project, "searchTerm": title, "limit": 50,
                "sourceKinds": ["cli", "vscode", "appServer", "exec", "unknown"],
                "useStateDbOnly": True,
            })
            candidates = [x for x in listing.get("data", [])
                          if x.get("name") == title and
                          normalized_project(x.get("cwd", "")) == normalized_project(project)]
            if len(candidates) != 1 or listing.get("nextCursor"):
                raise HistoryError("selection_not_unique")
            thread_id = candidates[0]["id"]
        metadata = client.request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
        if normalized_project(metadata.get("cwd", "")) != normalized_project(project):
            raise HistoryError("project_scope_mismatch")
        history = client.request("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]
        if normalized_project(history.get("cwd", "")) != normalized_project(project):
            raise HistoryError("project_scope_mismatch")
        turns = history.get("turns", [])
        messages, used, truncated = [], 0, len(turns) > turn_limit
        for turn in turns[-turn_limit:]:
            for item in turn.get("items", []):
                if item.get("type") == "userMessage":
                    text = "\n".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "text")
                    role = "user"
                elif item.get("type") == "agentMessage":
                    text, role = item.get("text", ""), "assistant"
                else:
                    continue
                if not text:
                    continue
                remaining = text_limit - used
                if remaining <= 0:
                    truncated = True
                    continue
                truncated = truncated or len(text) > remaining
                text = text[:remaining]
                used += len(text)
                messages.append({"role": role, "text": text, "turn_id": turn.get("id"), "phase": item.get("phase")})
        return {"status": "PASS", "transport": "stdio", "project": project,
                "thread_id": thread_id, "title": history.get("name"),
                "turns_available": len(turns), "truncated": truncated,
                "content_is_untrusted_source": True, "messages": messages}
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-cli", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--title")
    selection.add_argument("--thread-id")
    parser.add_argument("--turn-limit", type=int, default=6)
    parser.add_argument("--text-limit", type=int, default=30000)
    args = parser.parse_args()
    if not args.codex_cli.is_file() or not args.project.is_absolute():
        parser.error("A trusted native Codex executable and absolute project directory are required.")
    try:
        result = read_history([str(args.codex_cli.resolve()), "app-server", "--listen", "stdio://"],
                              args.project, title=args.title, thread_id=args.thread_id,
                              turn_limit=args.turn_limit, text_limit=args.text_limit)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except HistoryError as error:
        print(json.dumps({"status": "FAIL", "category": str(error)}))
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "FAIL", "category": "history_read_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
