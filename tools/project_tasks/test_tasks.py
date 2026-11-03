"""Fault-boundary contracts, actual process death and real read-only files."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.project_tasks import engine as e, store, runner, transport
from project_records import ProjectRecords
from test_chat_client import capabilities, whole

BASE = store.ROOT / ".q4t-work/serve-project-tasks-20261104"
BINDING = dict(client_id="alpha", base_url="https://q4t.internal.test:18448/v1",
    connect_host="127.0.0.1", model="qwen3.8-flash-next",
    protocol_sha256="a" * 64, template_sha256="b" * 64,
    ca_sha256="c" * 64, key_sha256="d" * 64)


def preflight_response(request, *, output_limit=512):
    return dict(object="q4t.chat.preflight", profile="q4t-preflight-v1",
        model=request["model"], protocol_source_sha256=BINDING["protocol_sha256"],
        prompt_tokens=100, max_context_tokens=8192,
        output=dict(source="max_tokens", requested_tokens=request["max_tokens"],
            capped_tokens=request["max_tokens"], available_tokens=request["max_tokens"],
            counts_reasoning_and_forced_tokens=True),
        admission=dict(policy="explicit_full_budget", required_capacity=100 + request["max_tokens"],
            eligible_slots=[0], slot_reserved=False, availability_checked=False),
        checks=dict(input=True, template=True, tokenizer=True, budget=True,
            structured_requested=True, grammar_compiled=False, cache_accessed=False,
            request_registered=False, gpu_executed=False), execution_guaranteed=False,
        client_limits=dict(max_prompt_tokens=8192, max_output_tokens=output_limit))


class FakeClient:
    def __init__(self, *, bad_tool=False, fail=False, bad_citation=False,
                 finish=None, path="docs/current.md", batch=False):
        self.calls, self.cap_calls = [], 0
        self.preflight_calls = []
        self.bad_tool, self.fail, self.bad_citation = bad_tool, fail, bad_citation
        self.finish, self.path, self.batch = finish, path, batch

    def capabilities(self, **kwargs):
        self.cap_calls += 1
        caps = capabilities()
        caps["tools"].update(strict=True, tool_choice=["auto", "none", "required"],
                             parallel_tool_calls=[True, False])
        return caps

    def complete(self, request, *, sink):
        self.calls.append(deepcopy(request))
        if self.fail:
            sink(b'{"partial":')
            raise ConnectionError("injected stopped transport")
        results = [json.loads(m["content"]) for m in request["messages"] if m["role"] == "tool"]
        if results:
            result = results[-1]
            text = (result["source"]["text"] + " " + result["source"]["citation"]
                    if result["ok"] else "无法核实：" + result["error"]["code"])
            if self.bad_citation:
                text += " [docs/unread.md:L1-L3]"
            message, finish = dict(role="assistant", content=text), "stop"
        else:
            calls = [dict(id="read-" + request["request_id"], type="function",
                function=dict(name="read_project_record", arguments=json.dumps(
                    dict(path=self.path, start_line=1, line_count=3))))]
            if self.batch:
                calls.append(dict(id="second-" + request["request_id"], type="function",
                    function=dict(name="read_project_record", arguments=json.dumps(
                        dict(path=self.path, start_line=1, line_count=3)))))
            if self.bad_tool:
                calls[-1]["function"]["name"] = "execute_shell"
            message, finish = dict(role="assistant", content=None, tool_calls=calls), "tool_calls"
        raw = json.dumps(whole(message, self.finish or finish, request["request_id"]),
                         ensure_ascii=False).encode()
        sink(raw)
        return e.chat.read_json(io.BytesIO(raw))

    def preflight(self, request, *, sink):
        self.preflight_calls.append(deepcopy(request))
        value = preflight_response(request)
        sink(store.encode(value))
        return value


class CountedRecords(ProjectRecords):
    def execute(self, invocation):
        path = self.root / "executions.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(dict(name=invocation.name, arguments=invocation.arguments)) + "\n")
            stream.flush(); os.fsync(stream.fileno())
        return super().execute(invocation)


class Tasks(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        (BASE / "fixtures").mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=BASE / "fixtures"))
        self.root = self.directory / "store"
        self.project = self.directory / "project"
        (self.project / "docs").mkdir(parents=True)
        (self.project / "docs/current.md").write_text("initial value\nsource two\nsource three\n")
        store.init(self.root)
        self.records = CountedRecords(self.project)
        self.binding = deepcopy(BINDING)
        self.name = store.task_name(self.binding["client_id"], "key")
        self.spec = e.specification(self.binding, "read current", stream=False)
        store.create(self.root, self.name, self.spec)

    def count(self):
        path = self.project / "executions.jsonl"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def execute(self, client=None, checkpoint=None):
        with store.open_task(self.root, self.name) as ledger:
            return e.run(ledger, self.binding, client or FakeClient(),
                         records=self.records, checkpoint=checkpoint)

    def status(self):
        return runner.inspect(self.root, self.binding, "key")

    def break_at(self, name):
        def checkpoint(kind, *args):
            if kind == name:
                raise InterruptedError("explicit CPU boundary")
        with self.assertRaises(InterruptedError):
            self.execute(checkpoint=checkpoint)

    def resolve(self, decision, revision=None):
        state = self.status()
        return runner.resolve(self.root, self.binding, "key", decision,
            revision or state["revision"], "explicit contract decision")

    def test_complete_then_duplicate_is_read_only(self):
        result = self.execute()
        self.assertEqual((result["state"], result["outcome"], self.count()),
                         ("COMPLETED", "ANSWERED", 1))
        fake = FakeClient(fail=True)
        again = runner.submit(self.root, self.binding, "key", "read current", fake, stream=False)
        self.assertFalse(again["created"])
        self.assertEqual(again["answer"], result["answer"])
        self.assertEqual((fake.cap_calls, len(fake.calls), self.count()), (0, 0, 1))
        self.assertEqual(self.status()["revision"], result["revision"])

    def test_key_conflict_scope_and_config_binding(self):
        with self.assertRaisesRegex(ValueError, "conflicts"):
            runner.submit(self.root, self.binding, "key", "different", FakeClient(), stream=False)
        beta = dict(self.binding, client_id="beta")
        other = store.task_name(beta["client_id"], "key")
        self.assertNotEqual(other, self.name)
        store.create(self.root, other, e.specification(beta, "read current", stream=False))
        with store.open_task(self.root, other) as ledger:
            with self.assertRaisesRegex(ValueError, "binding"):
                e.run(ledger, self.binding, FakeClient())
        with store.open_task(self.root, self.name) as ledger:
            with self.assertRaisesRegex(ValueError, "runtime changed"):
                with patch.object(e, "sources", return_value={}):
                    e.run(ledger, self.binding, FakeClient())

    def test_saved_tool_result_reused_after_source_changes(self):
        self.break_at("tool_result")
        (self.project / "docs/current.md").write_text("new value\n")
        result = self.execute()
        self.assertEqual((result["state"], result["requests"], self.count()), ("COMPLETED", 2, 1))
        self.assertIn("initial value", result["answer"])
        self.assertNotIn("new value", result["answer"])

    def test_unknown_read_requires_explicit_new_read_and_keeps_attempt(self):
        self.break_at("tool_returned")
        state = self.status()
        self.assertEqual((state["state"], self.count()), ("TOOL_UNKNOWN", 1))
        fake = FakeClient()
        self.assertEqual(self.execute(fake)["state"], "TOOL_UNKNOWN")
        self.assertEqual((fake.cap_calls, len(fake.calls), self.count()), (0, 0, 1))
        with self.assertRaises(ValueError):
            self.resolve("repeat-read", "0" * 64)
        self.assertEqual(self.status()["revision"], state["revision"])
        (self.project / "docs/current.md").write_text("new observed value\n")
        self.resolve("repeat-read")
        result = self.execute()
        self.assertEqual((result["tool_attempts"], result["saved_tool_results"],
                          result["read_repetitions"], self.count()), (2, 1, 1, 2))
        self.assertIn("new observed value", result["answer"])

    def test_unknown_request_discard_consumes_budget_fresh_ids_and_keeps_wire(self):
        fake = FakeClient(fail=True)
        with self.assertRaises(ConnectionError):
            self.execute(fake)
        path = self.root / "tasks" / self.name / "wire-001.bin"
        original = path.read_bytes()
        state = self.status()
        self.assertEqual(state["state"], "REQUEST_UNKNOWN")
        self.assertEqual(self.execute()["revision"], state["revision"])
        self.resolve("discard-request")
        good = FakeClient()
        result = self.execute(good)
        self.assertEqual((result["state"], result["requests"], result["discarded_requests"]),
                         ("COMPLETED", 3, 1))
        self.assertNotEqual(fake.calls[0]["request_id"], good.calls[0]["request_id"])
        self.assertNotEqual(fake.calls[0]["cancel_token"], good.calls[0]["cancel_token"])
        self.assertEqual(path.read_bytes(), original)

    def test_whole_tool_batch_checked_before_any_execution(self):
        result = self.execute(FakeClient(batch=True, bad_tool=True))
        self.assertEqual((result["state"], self.count()), ("FAILED", 0))
        self.assertEqual(result["outcome"], "response_or_business_contract")

    def test_length_and_bad_citation_never_succeed(self):
        result = self.execute(FakeClient(finish="length"))
        self.assertEqual((result["state"], self.count()), ("FAILED", 0))
        name = store.task_name("alpha", "bad-citation")
        store.create(self.root, name, self.spec)
        with store.open_task(self.root, name) as ledger:
            result = e.run(ledger, self.binding, FakeClient(bad_citation=True), records=self.records)
        self.assertEqual(result["state"], "FAILED")

    def test_expected_source_error_is_unverified_business_result(self):
        result = self.execute(FakeClient(path="docs/absent.md"))
        self.assertEqual((result["state"], result["outcome"]), ("COMPLETED", "UNVERIFIED"))
        self.assertIn("record_not_found", result["answer"])

    def test_wires_and_event_corruption_refuse_resume(self):
        self.break_at("response")
        path = self.root / "tasks" / self.name / "wire-001.bin"
        path.write_bytes(path.read_bytes().replace(b"current.md", b"foreign.md"))
        with self.assertRaisesRegex(ValueError, "wire/response"):
            self.execute()
        with store.open_task(self.root, self.name) as ledger:
            ledger.db.execute("UPDATE events SET payload='{}' WHERE seq=1")
        with self.assertRaisesRegex(ValueError, "event content"):
            self.status()

    def test_private_files_links_and_task_capacity(self):
        with store.open_task(self.root, self.name) as ledger:
            db = ledger.path / "ledger.db"
            os.link(db, self.directory / "hardlink")
        with self.assertRaises(ValueError):
            self.status()
        (self.directory / "hardlink").unlink()
        small = self.directory / "small"
        store.init(small, 1)
        store.create(small, self.name, self.spec)
        with self.assertRaisesRegex(ValueError, "capacity"):
            store.create(small, "f" * 64, self.spec)
        self.assertEqual(len(list((small / "tasks").iterdir())), 1)
        with self.assertRaises(FileExistsError):
            store.init(small)
        (small / "tasks" / self.name / ".lock").chmod(0o644)
        with self.assertRaises(ValueError):
            with store.open_task(small, self.name):
                pass

    def test_explicit_resolution_cannot_extend_budgets(self):
        name = store.task_name("alpha", "limited")
        spec = e.specification(self.binding, "read current", max_requests=2,
                               max_tools=1, stream=False)
        store.create(self.root, name, spec)
        with store.open_task(self.root, name) as ledger:
            def interrupted(kind, *args):
                if kind == "tool_returned":
                    raise InterruptedError()
            with self.assertRaises(InterruptedError):
                e.run(ledger, self.binding, FakeClient(), records=self.records, checkpoint=interrupted)
            state = e.replay(ledger)
            with self.assertRaises(ValueError):
                e.resolve(ledger, self.binding, "repeat-read", state["revision"], "budget test")
            abandoned = e.resolve(ledger, self.binding, "abandon", state["revision"], "cannot safely finish")
            self.assertEqual(abandoned["state"], "ABANDONED")
            self.assertEqual(e.run(ledger, self.binding, FakeClient())["state"], "ABANDONED")

    def test_capability_failure_precedes_any_submission(self):
        fake = FakeClient()
        with patch.object(fake, "capabilities", return_value=capabilities()):
            with self.assertRaisesRegex(ValueError, "strict tool"):
                self.execute(fake)
        self.assertEqual((self.status()["requests"], len(fake.calls)), (0, 0))

    def child(self, boundary, *, transaction=False):
        marker = self.directory / (boundary + "-ready.json")
        command = [sys.executable, "-B", str(store.ROOT / "tools/project_tasks/test_worker.py"),
                   str(self.root), self.name, str(self.project), boundary, str(marker)]
        if transaction:
            command.append("transaction")
        log = (self.directory / (boundary + "-child.log")).open("xb")
        self.addCleanup(log.close)
        process = subprocess.Popen(command, cwd=store.ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        def cleanup():
            if process.poll() is None:
                process.kill(); process.wait(5)
        self.addCleanup(cleanup)
        until = time.monotonic() + 8
        while time.monotonic() < until and not marker.exists() and process.poll() is None:
            time.sleep(.02)
        self.assertTrue(marker.exists(), (process.poll(), str(self.directory)))
        self.assertIsNone(process.poll())
        return process, marker

    def test_actual_process_death_saved_result_then_new_process_resume(self):
        process, marker = self.child("tool_result")
        self.assertEqual(self.status()["state"], "ACTIVE")
        with self.assertRaises(BlockingIOError):
            runner.submit(self.root, self.binding, "key", "read current", FakeClient(), stream=False)
        process.kill(); self.assertEqual(process.wait(5), -signal.SIGKILL)
        before = json.loads(marker.read_text())
        resume = subprocess.run([sys.executable, "-B", str(store.ROOT / "tools/project_tasks/test_worker.py"),
            str(self.root), self.name, str(self.project), "finish", str(self.directory / "finished.json")],
            cwd=store.ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(resume.returncode, 0, resume.stderr)
        after = self.status()
        self.assertEqual((after["state"], self.count(), after["requests"]), ("COMPLETED", 1, 2))
        self.assertNotEqual(before["pid"], json.loads((self.directory / "finished.json").read_text())["pid"])

    def test_actual_process_death_unknown_read_is_not_reexecuted(self):
        process, _ = self.child("tool_returned")
        process.kill(); process.wait(5)
        self.assertEqual((self.status()["state"], self.count()), ("TOOL_UNKNOWN", 1))
        self.assertEqual(self.execute()["state"], "TOOL_UNKNOWN")
        self.assertEqual(self.count(), 1)

    def test_actual_wire_process_death_keeps_bytes_without_promoting_response(self):
        process, _ = self.child("wire")
        process.kill(); process.wait(5)
        path = self.root / "tasks" / self.name / "wire-001.bin"
        self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(self.status()["state"], "REQUEST_UNKNOWN")
        self.assertEqual(self.execute()["state"], "REQUEST_UNKNOWN")
        self.assertEqual(self.count(), 0)

    def test_actual_transaction_death_rolls_back_uncommitted_event(self):
        process, _ = self.child("transaction", transaction=True)
        process.kill(); process.wait(5)
        with store.open_task(self.root, self.name) as ledger:
            self.assertEqual(ledger.events(), [])
        self.assertEqual(self.execute()["state"], "COMPLETED")

    def test_independent_tasks_can_progress_while_one_writer_is_active(self):
        process, _ = self.child("tool_result")
        name = store.task_name("beta", "key")
        binding = dict(self.binding, client_id="beta")
        store.create(self.root, name, e.specification(binding, "read current", stream=False))
        with store.open_task(self.root, name) as ledger:
            result = e.run(ledger, binding, FakeClient(), records=self.records)
        self.assertEqual(result["state"], "COMPLETED")
        self.assertIsNone(process.poll())
        process.kill(); process.wait(5)

    def test_profile_is_private_tls_bound_and_secret_free_projection(self):
        ca = self.directory / "ca.pem"; ca.write_text("test certificate identity")
        path = self.directory / "profile.json"
        value = {k: BINDING[k] for k in ("client_id", "base_url", "connect_host", "model",
                                       "protocol_sha256", "template_sha256")}
        value.update(api_key="private-contract-key-" + "z" * 40, ca_file=str(ca))
        store.write_new(path, store.encode(value))
        _, binding = transport.profile(path)
        self.assertNotIn(value["api_key"], json.dumps(binding))
        for bad in (dict(value, base_url="http://localhost"),
                    dict(value, connect_host="8.8.8.8"), dict(value, api_key="a\nb" * 20)):
            path.write_bytes(store.encode(bad))
            with self.assertRaises(ValueError):
                transport.profile(path)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            transport.profile(path)

    def test_deadline_reader_checks_each_partial_read_and_eof(self):
        class Response:
            chunks = [b"first", b"\nsecond", b"", b"impossible"]
            def isclosed(self):
                return False
            def read1(self, size):
                return self.chunks.pop(0)
        class Sock:
            def settimeout(self, value):
                self.timeout = value
        reader = transport.DeadlineReader(Response(), Sock(), time.monotonic() + 2)
        self.assertEqual(reader.readline(100), b"first\n")
        self.assertEqual(reader.read(100), b"second")
        self.assertEqual(reader.readline(100), b"")
        reader = transport.DeadlineReader(Response(), Sock(), time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            reader.read(100)

    def test_content_length_eof_after_connection_close_does_not_touch_retired_socket(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        right.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nabc")
        response = transport.http.client.HTTPResponse(left)
        self.addCleanup(response.close)
        response.begin()
        left.close()  # HTTPConnection hands the remaining socket IO reference to response.
        reader = transport.DeadlineReader(response, left, time.monotonic() + 2)
        self.assertEqual(reader.read(100), b"abc")
        self.assertEqual(reader.read(100), b"")
        self.assertTrue(response.isclosed())


if __name__ == "__main__":
    unittest.main()
