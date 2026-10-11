import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

SPEC = importlib.util.spec_from_file_location("history_reader", Path(__file__).resolve().parents[1] / "scripts/read-codex-history.py")
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)

FAKE_SERVER = r'''
import json, sys, time
from pathlib import Path
config=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
log=Path(sys.argv[2])
for line in sys.stdin:
    request=json.loads(line)
    method=request["method"]
    with log.open("a",encoding="utf-8") as f:
        f.write(json.dumps({"method":method,"params":request.get("params")})+"\n")
    if "id" not in request: continue
    mode=config.get("mode", "normal")
    if mode=="hang": time.sleep(60)
    if mode=="oversize":
        print(json.dumps({"id":request["id"],"result":{"large":"x"*4096}}),flush=True)
        continue
    if mode=="error":
        print(json.dumps({"id":request["id"],"error":{"code":-1,"message":"private-error-fixture"}}),flush=True)
        continue
    if mode=="eof": break
    project=config["project"]
    if method=="initialize": result={"server":"fixture"}
    elif method=="thread/list":
        item={"id":"fixture-id","name":"fixture topic","cwd":project}
        result={"data":[item,item] if mode=="duplicate" else [item],"nextCursor":None}
    elif method=="thread/read":
        cwd=str(Path(project).parent) if mode=="foreign" else project
        thread={"id":"fixture-id","name":"fixture topic","cwd":cwd}
        if request["params"]["includeTurns"]:
            thread["turns"]=[{"id":"fixture-turn","items":[
                {"type":"userMessage","content":[{"type":"text","text":"Which storage was discussed?"}]},
                {"type":"agentMessage","text":"Fixture service A, B and C.","phase":"final_answer"},
                {"type":"reasoning","text":"private-reasoning-fixture"},
                {"type":"commandExecution","output":"private-tool-fixture"},
                {"type":"developerMessage","text":"untrusted-rule-fixture"}]}]
        result={"thread":thread}
    else: raise SystemExit(3)
    print(json.dumps({"method":"fixture/notification","params":{}}),flush=True)
    print(json.dumps({"id":request["id"],"result":result}),flush=True)
'''


class CodexHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.server = self.root / "server.py"
        self.server.write_text(FAKE_SERVER, encoding="utf-8")
        self.config = self.root / "config.json"
        self.log = self.root / "methods.jsonl"

    def command(self, mode="normal"):
        self.config.write_text(json.dumps({"project": str(self.project), "mode": mode}), encoding="utf-8")
        return [sys.executable, str(self.server), str(self.config), str(self.log)]

    def recorded(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_actual_protocol_is_read_only_and_returns_only_user_assistant(self):
        result = history.read_history(self.command(), self.project, title="fixture topic")
        self.assertEqual([m["role"] for m in result["messages"]], ["user", "assistant"])
        self.assertEqual(result["messages"][1]["text"], "Fixture service A, B and C.")
        self.assertTrue(result["content_is_untrusted_source"])
        self.assertFalse(result["truncated"])
        self.assertNotIn("private-", json.dumps(result))
        records = self.recorded()
        self.assertEqual([r["method"] for r in records], ["initialize", "initialized", "thread/list", "thread/read", "thread/read"])
        self.assertEqual([r["params"]["includeTurns"] for r in records if r["method"] == "thread/read"], [False, True])
        self.assertTrue(records[2]["params"]["useStateDbOnly"])

    def test_foreign_project_is_rejected_before_history_body(self):
        with self.assertRaisesRegex(history.HistoryError, "project_scope_mismatch"):
            history.read_history(self.command("foreign"), self.project, thread_id="fixture-id")
        self.assertFalse(any(r["params"].get("includeTurns") for r in self.recorded() if r["method"] == "thread/read"))

    def test_duplicate_title_does_not_guess_or_read_body(self):
        with self.assertRaisesRegex(history.HistoryError, "selection_not_unique"):
            history.read_history(self.command("duplicate"), self.project, title="fixture topic")
        self.assertNotIn("thread/read", [r["method"] for r in self.recorded()])

    def test_inference_or_mutation_methods_are_rejected(self):
        client = history.ReadClient(self.command())
        try:
            for method in ["turn/start", "thread/resume", "thread/start", "thread/archive"]:
                with self.assertRaisesRegex(history.HistoryError, "method_not_read_only"):
                    client.request(method, {})
        finally:
            client.close()
        self.assertIsNotNone(client.process.poll())

    def test_timeout_terminates_only_owned_child(self):
        client = history.ReadClient(self.command("hang"), timeout=0.15)
        try:
            with self.assertRaisesRegex(history.HistoryError, "rpc_timeout"):
                client.initialize()
        finally:
            client.close()
        self.assertIsNotNone(client.process.poll())

    def test_oversize_response_is_rejected(self):
        client = history.ReadClient(self.command("oversize"), max_response=1024)
        try:
            with self.assertRaisesRegex(history.HistoryError, "response_limit"):
                client.initialize()
        finally:
            client.close()

    def test_early_eof_is_bounded(self):
        with self.assertRaisesRegex(history.HistoryError, "rpc_eof"):
            history.read_history(self.command("eof"), self.project, title="fixture topic", timeout=2)

    def test_server_error_does_not_expose_private_diagnostics(self):
        with self.assertRaisesRegex(history.HistoryError, "^rpc_rejected$"):
            history.read_history(self.command("error"), self.project, title="fixture topic")

    def test_invalid_scope_or_limit_does_not_launch(self):
        for kwargs in [{"title":"fixture topic","thread_id":"fixture-id"}, {"turn_limit":0}]:
            with self.assertRaisesRegex(history.HistoryError, "invalid_"):
                history.read_history(["no-such-executable"], self.project, **kwargs)


if __name__ == "__main__":
    unittest.main()
