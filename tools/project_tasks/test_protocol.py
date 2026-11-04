"""Reviewed upgrades preserve actual history, uncertainty and permanent keys."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from tools.project_tasks import engine as e, store, migration as m, protocol as p
from tools.project_tasks import runner, retention as r, backup, transport
from tools.project_tasks import test_tasks as old, test_archive, test_errors, migration_fixtures as f

BASE = store.ROOT / ".q4t-work/serve-task-protocol-20261104"
REVIEW = json.loads((store.ROOT / p.REGISTRY).read_bytes())["releases"][0]
BEFORE, AFTER = REVIEW["from_protocol_sha256"], REVIEW["to_protocol_sha256"]


class Client(old.FakeClient):
    def __init__(self, binding):
        super().__init__()
        self.binding = binding

    def capabilities(self, **kwargs):
        value = super().capabilities(**kwargs)
        value["protocol_source_sha256"] = self.binding["protocol_sha256"]
        value["template"]["reference_sha256"] = self.binding["template_sha256"]
        return value

    def preflight(self, body, *, sink):
        self.preflight_calls.append(deepcopy(body))
        value = old.preflight_response(body)
        value["protocol_source_sha256"] = self.binding["protocol_sha256"]
        sink(store.encode(value))
        return value


class Protocol(unittest.TestCase):
    count = old.Tasks.count
    status = old.Tasks.status
    break_at = old.Tasks.break_at

    def setUp(self):
        old.Tasks.setUp(self)
        self.binding.update(protocol_sha256=BEFORE, template_sha256=REVIEW["template_sha256"])
        self.root = self.directory / "protocol-store"
        store.init(self.root)
        self.spec = e.specification(self.binding, "read current", stream=False)
        store.create(self.root, self.name, self.spec)

    def execute(self, client=None, checkpoint=None):
        return runner.resume(self.root, self.binding, "key", client or Client(self.binding),
            records=self.records, checkpoint=checkpoint)

    def plan(self, binding=None, label="protocol"):
        self.target = binding or dict(self.binding, protocol_sha256=AFTER)
        self.path = self.directory / (label + ".json")
        self.prepared = m.prepare(self.root, self.target, "key", self.path, "reviewed protocol upgrade", protocol=True)
        return self.prepared

    def apply(self):
        return m.apply(self.root, self.target, "key", self.path, self.prepared["plan_sha256"], protocol=True)

    def test_directed_registry_rejects_unknown_reverse_model_template_and_mixed_identity(self):
        target = dict(self.binding, protocol_sha256=AFTER)
        self.assertEqual(p.reviewed(self.binding, target), REVIEW)
        for source, dest in ((target, self.binding), (self.binding, dict(target, protocol_sha256="f"*64)),
            (dict(self.binding, model="other"), dict(target, model="other")),
            (dict(self.binding, template_sha256="e"*64), dict(target, template_sha256="e"*64))):
            with self.assertRaises(store.ContractError) as caught: p.reviewed(source, dest)
            self.assertEqual(caught.exception.code, "protocol_unsupported")
        for key, value in (("client_id", "beta"), ("base_url", "https://other/v1"),
            ("connect_host", "127.0.0.2"), ("key_sha256", "e"*64), ("ca_sha256", "e"*64),
            ("model", "other"), ("template_sha256", "e"*64), ("protocol_sha256", "bad")):
            with self.subTest(key=key), self.assertRaises(store.ContractError):
                p.same_identity(self.binding, dict(target, **{key:value}))
        with self.assertRaises(store.ContractError): p.same_identity(self.binding, self.binding)

    def test_saved_tool_wire_history_budget_and_duplicate_execution(self):
        self.break_at("tool_result")
        with store.open_task(self.root, self.name) as ledger:
            meta, events, raw = deepcopy(ledger.meta), ledger.events(), ledger.wire(1).read_bytes()
        self.plan(); moved = self.apply()
        self.assertEqual((moved["requests"], moved["saved_tool_results"], moved["migrations"]), (1, 1, 1))
        self.assertTrue(self.prepared["protocol_changed"])
        self.assertFalse(self.prepared["key_changed"] or self.prepared["ca_changed"])
        with store.open_task(self.root, self.name) as ledger:
            self.assertEqual((ledger.meta, ledger.events()[:-1], ledger.wire(1).read_bytes()), (meta, events, raw))
        with self.assertRaises(store.ContractError): self.status()
        self.binding = self.target
        (self.project / "docs/current.md").write_text("changed after saved read\n")
        client = Client(self.binding); result = self.execute(client)
        self.assertEqual((result["state"], result["requests"], self.count()), ("COMPLETED", 2, 1))
        self.assertEqual((len(client.calls), len(client.preflight_calls)), (1, 1))
        self.assertIn("initial value", result["answer"])
        again = runner.submit(self.root, self.binding, "key", "read current", client, stream=False)
        self.assertEqual(again, dict(result, created=False)); self.assertTrue(self.apply()["already_applied"])
        self.assertEqual(len(client.calls), 1)

    def test_unknown_request_and_tool_stay_unknown_without_network_or_read(self):
        for boundary in ("request", "tool_started"):
            self.setUp(); self.break_at(boundary); self.plan(); moved = self.apply()
            client = Client(self.target); result = runner.resume(self.root, self.target, "key", client)
            self.assertEqual(result["state"], "REQUEST_UNKNOWN" if boundary == "request" else "TOOL_UNKNOWN")
            self.assertEqual(result["revision"], moved["revision"])
            self.assertEqual((client.cap_calls, client.calls, client.preflight_calls, self.count()), (0, [], [], 0))

    def test_saved_approval_resets_and_rechecks_new_protocol(self):
        self.break_at("preflight_result"); self.plan(); self.apply(); self.binding = self.target
        client = Client(self.binding); result = self.execute(client)
        self.assertEqual((result["state"], result["preflight_checks"], len(client.preflight_calls)), ("COMPLETED", 3, 2))
        with store.open_task(self.root, self.name) as ledger:
            self.assertEqual(json.loads(e.check_wire(ledger, 1).read_bytes())["protocol_source_sha256"], BEFORE)
            self.assertEqual(json.loads(e.check_wire(ledger, 2).read_bytes())["protocol_source_sha256"], AFTER)

    def test_terminal_results_stay_final_and_registry_removal_does_not_break_history(self):
        done = self.execute(); self.plan(); moved = self.apply(); self.binding = self.target
        with patch.object(p, "REGISTRY", "absent-registry.json"):
            client = Client(self.binding); result = self.execute(client)
            self.assertTrue(self.apply()["already_applied"])
        self.assertEqual((result["answer"], result["requests"], result["revision"]),
            (done["answer"], done["requests"], moved["revision"]))
        self.assertEqual((client.cap_calls, client.calls, self.count()), (0, [], 1))

    def test_target_service_capability_mismatch_refuses_before_generation(self):
        self.break_at("tool_result"); before = deepcopy(self.binding)
        self.plan(); self.apply(); self.binding = self.target
        client = Client(before)
        with self.assertRaises(ValueError): self.execute(client)
        self.assertEqual((client.calls, client.preflight_calls, self.count()), ([], [], 1))
        self.assertEqual(self.status()["requests"], 1)

    def test_format_digest_stale_and_mixed_modes_rejected(self):
        self.plan()
        for options in ({}, {"trust":True}, {"trust":True,"protocol":True}):
            with self.assertRaises(store.ContractError):
                m.apply(self.root, self.target, "key", self.path, self.prepared["plan_sha256"], **options)
        with self.assertRaises(store.ContractError): m.apply(self.root, self.target, "key", self.path, "0"*64, protocol=True)
        for options in ({}, {"trust":True}):
            with self.assertRaises(store.ContractError): m.prepare(self.root, self.target, "key", self.directory/"wrong.json", "wrong route", **options)
        self.execute()
        with self.assertRaises(store.ContractError) as caught: self.apply()
        self.assertEqual(caught.exception.code, "plan_stale")

    def test_plan_review_budget_sources_wire_and_exclusive_owner_are_bound(self):
        self.break_at("request"); self.plan(); original = json.loads(self.path.read_bytes())
        for field, value in (("max_tokens",64), ("question","other"), ("max_requests",8)):
            changed = deepcopy(original); changed["target_spec"][field] = value
            self.path.write_bytes(store.encode(changed))
            with self.assertRaises(store.ContractError): m.apply(self.root,self.target,"key",self.path,store.digest(changed),protocol=True)
        changed = deepcopy(original); changed["protocol_review"]["archive_sha256"] = "f"*64
        self.path.write_bytes(store.encode(changed))
        with self.assertRaises(store.ContractError): m.apply(self.root,self.target,"key",self.path,store.digest(changed),protocol=True)
        self.path.write_bytes(store.encode(original))
        with store.open_task(self.root,self.name), self.assertRaises(BlockingIOError): self.apply()
        with patch.object(e,"sources",return_value={}), self.assertRaises(store.ContractError): self.apply()
        store.write_new(self.root/"tasks"/self.name/"wire-001.bin", b"partial")
        with self.assertRaises(store.ContractError) as caught: self.apply()
        self.assertEqual(caught.exception.code,"plan_stale")

    def test_existing_eight_migrations_shared_with_key_and_ca(self):
        self.plan(); self.apply(); self.binding=self.target
        for i in range(7):
            trust=bool(i%2); field="ca_sha256" if trust else "key_sha256"
            target=dict(self.binding,**{field:hashlib.sha256(str(i).encode()).hexdigest()})
            path=self.directory/f"mixed-{i}.json"; prepared=m.prepare(self.root,target,"key",path,"bounded",trust=trust)
            value=m.apply(self.root,target,"key",path,prepared["plan_sha256"],trust=trust); self.binding=target
            self.assertEqual((value["migrations"],value["runs"]),(i+2,0))
        with self.assertRaises(store.ContractError) as caught:
            m.prepare(self.root,dict(self.binding,key_sha256="f"*64),"key",self.directory/"over.json","limit")
        self.assertEqual(caught.exception.code,"task_budget")

    def test_sigkill_active_before_and_after_commit(self):
        for point in ("before_commit","after_commit"):
            self.setUp(); self.break_at("tool_result"); self.plan(); marker=self.directory/"marker.json"
            script="""import os,sys,threading,json
from pathlib import Path
from tools.project_tasks import store,migration
root,path,digest,point,marker=sys.argv[1:]
plan=json.loads(Path(path).read_bytes())
def hook(kind,ledger):
 if kind==point:
  store.write_new(Path(marker),store.encode(dict(pid=os.getpid(),point=point)))
  threading.Event().wait()
migration.apply(root,plan['target_spec']['binding'],'key',path,digest,protocol=True,checkpoint=hook)
"""
            child=subprocess.Popen([sys.executable,"-B","-c",script,str(self.root),str(self.path),self.prepared["plan_sha256"],point,str(marker)],cwd=store.ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                end=time.monotonic()+5
                while not marker.exists() and child.poll() is None and time.monotonic()<end: time.sleep(.01)
                self.assertTrue(marker.exists()); child.kill(); child.communicate(timeout=5)
                self.assertEqual(child.returncode,-signal.SIGKILL)
                store.write_new(self.directory/("killed-protocol-"+point+".json"),store.encode(dict(point=point,pid=child.pid,returncode=child.returncode)))
                value=self.apply(); self.assertEqual(value["already_applied"],point=="after_commit"); self.assertEqual(value["saved_tool_results"],1)
            finally:
                if child.poll() is None: child.kill(); child.communicate(timeout=5)

    def test_seven_historical_source_snapshots_and_actual_11g_final_ledger(self):
        for index in range(7):
            root=self.directory/f"old-{index}"; f.extract_runtime(index,root)
            self.assertEqual(m.supported(dict(runtime_sources=f.release(index)["runtime_sources"],
                budget_profile=f.release(index)["budget_profile"]),e.sources()),f.release(index)["release"])
        data=f.archive(6); exported=json.loads(data["execution/alpha-ledger.json"])
        directory=self.directory/"history"; directory.mkdir(mode=0o700)
        for name,raw in data.items():
            if name.startswith("execution/alpha-completed-task/") and Path(name).name.startswith(("wire-","check-")):
                store.write_new(directory/Path(name).name,raw)
        class Ledger:
            meta=exported["meta"]; spec=meta["spec"]; initial=store.digest(meta); path=directory
            def wire(self,number): return self.path/f"wire-{number:03d}.bin"
            def events(self): return exported["events"]
        previous=store.digest(exported["meta"])
        for row in exported["events"]:
            self.assertEqual(row["digest"],store.digest([row["seq"],row["kind"],row["payload"],previous])); previous=row["digest"]
        result=e.replay(Ledger())
        self.assertEqual((result["phase"],result["requests"],result["tool_results"]),("COMPLETED",2,1))
        self.assertEqual(result["execution_spec"]["runtime_sources"],f.release(6)["runtime_sources"])
        proof=store.ROOT/REVIEW["evidence"]
        self.assertEqual(hashlib.sha256(proof.read_bytes()).hexdigest(),REVIEW["archive_sha256"])


class Archived(Protocol):
    def archived(self,prune=False):
        done=self.execute(); r.enable(self.root,min_retain_seconds=0)
        for kind in (("archive","prune") if prune else ("archive",)):
            path=self.directory/(kind+".json"); plan=r.prepare(self.root,self.binding,"key",kind,path,"terminal fixture")
            r.apply(self.root,self.binding,"key",path,plan["plan_sha256"])
        return done

    def test_retained_and_purged_key_contract_cannot_reexecute_or_change_question(self):
        for prune in (False,True):
            self.setUp(); done=self.archived(prune); path=self.root/"retention"; _,meta=store.checked_root(self.root)
            before,_=r.load_key(path,meta,self.name); files=backup.regular_tree(path/"payloads")
            self.plan(); value=self.apply()
            self.assertEqual(value["payload_state"],"purged" if prune else "retained")
            after,_=r.load_key(path,meta,self.name)
            self.assertEqual(before["base"],after["base"]); self.assertEqual(files,backup.regular_tree(path/"payloads"))
            client=Client(self.target)
            with patch.object(p,"REGISTRY","absent.json"):
                self.assertFalse(runner.submit(self.root,self.target,"key","read current",client,stream=False)["created"])
                self.assertEqual(runner.resume(self.root,self.target,"key",client)["state"],"ARCHIVED")
                self.assertTrue(self.apply()["already_applied"])
            for options in (dict(question="changed",stream=False),dict(question="read current",stream=False,max_tokens=64)):
                with self.assertRaises(store.ContractError): runner.submit(self.root,self.target,"key",client=client,**options)
            with self.assertRaises(store.ContractError): runner.resume(self.root,self.binding,"key",client)
            restored=r.lookup(self.root,self.target,"key",include_answer=True)
            if not prune: self.assertEqual(restored["answer"],done["answer"])
            self.assertEqual((client.cap_calls,client.calls,self.count()),(0,[],1))

    def test_archive_format_reverse_review_and_eight_operation_limit(self):
        self.archived(); self.plan()
        for options in ({},{"trust":True}):
            with self.assertRaises(store.ContractError): m.apply(self.root,self.target,"key",self.path,self.prepared["plan_sha256"],**options)
        self.apply(); old_binding=self.binding; self.binding=self.target
        with self.assertRaises(store.ContractError) as caught: self.plan(old_binding,"reverse")
        self.assertEqual(caught.exception.code,"protocol_unsupported")
        for i in range(7):
            target=dict(self.binding,ca_sha256=hashlib.sha256(str(i).encode()).hexdigest()); path=self.directory/f"ca-{i}.json"
            plan=m.prepare(self.root,target,"key",path,"mixed bounded",trust=True)
            m.apply(self.root,target,"key",path,plan["plan_sha256"],trust=True); self.binding=target
        with self.assertRaises(store.ContractError) as caught:
            m.prepare(self.root,dict(self.binding,key_sha256="f"*64),"key",self.directory/"limit.json","limit")
        self.assertEqual(caught.exception.code,"task_budget")

    def test_backup_restore_preserves_review_and_rebased_payload_identity(self):
        self.archived(); self.plan(); self.apply(); snapshot=self.directory/"snapshot"
        saved=backup.snapshot(self.root,snapshot); path=self.directory/"restore.json"; dest=self.directory/"restored"
        plan=backup.restore_plan(self.root,snapshot,saved["snapshot_sha256"],dest,path,"reviewed same-host handoff")
        backup.restore_apply(path,plan["plan_sha256"])
        result=r.lookup(dest,self.target,"key",include_answer=True)
        self.assertEqual(result["terminal_state"],"COMPLETED"); self.assertIn("initial value",result["answer"])
        with self.assertRaises(store.ContractError): r.lookup(self.root,self.target,"key")

    def test_sigkill_archive_before_and_after_commit_explicit_finish(self):
        for point in ("metadata_before_commit","key_published"):
            self.setUp(); self.archived(); self.plan(); self.plan_path=self.path
            marker=test_archive.Archives.kill(self,point)
            if point=="metadata_before_commit": self.apply()
            else:
                with self.assertRaises(store.ContractError): r.lookup(self.root,self.target,"key")
                r.finish(self.root,marker["operation_sha256"])
            self.assertTrue(self.apply()["already_applied"])
            self.assertEqual(r.lookup(self.root,self.target,"key")["payload_state"],"retained")


class Commands(test_errors.Errors):
    def test_protocol_cli_no_execution_and_redacted_errors(self):
        value=json.loads(self.profile.read_bytes()); value.update(protocol_sha256=BEFORE,template_sha256=REVIEW["template_sha256"])
        self.profile.write_bytes(store.encode(value)); _,binding=transport.profile(self.profile)
        self.root=self.directory/"protocol-cli"; store.init(self.root)
        store.create(self.root,self.name,e.specification(binding,"read current",stream=False))
        value["protocol_sha256"]=AFTER; self.profile.write_bytes(store.encode(value))
        before=backup.regular_tree(self.root)
        self.cli("plan-protocol","invalid_arguments",extra=("--plan",self.directory/"missing.json"))
        self.assertEqual(before,backup.regular_tree(self.root))
        path=self.directory/"protocol.json"
        plan=json.loads(self.cli("plan-protocol",extra=("--plan",path,"--reason","reviewed"),expected=0).stdout)
        self.cli("apply-trust","plan_changed",extra=("--plan",path,"--plan-sha256",plan["plan_sha256"]))
        result=json.loads(self.cli("apply-protocol",extra=("--plan",path,"--plan-sha256",plan["plan_sha256"]),expected=0).stdout)
        self.assertEqual((result["requests"],result["no_execution"],self.count()),(0,True,0))
        value["protocol_sha256"]=BEFORE; self.profile.write_bytes(store.encode(value))
        self.cli("plan-protocol","protocol_unsupported",extra=("--plan",self.directory/"reverse.json","--reason","unsupported"))


def load_tests(loader,tests,pattern):
    suite=loader.loadTestsFromTestCase(Protocol)
    for cls in (Archived,Commands):
        for name in cls.__dict__:
            if name.startswith("test_"): suite.addTest(cls(name))
    return suite
