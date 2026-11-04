"""Regression for profile alias export and explicit no-execution tripwires."""
import socket
import unittest
from tools.project_tasks import test_tasks as old, error_finish as f, error_noexec, store, engine


class Finish(unittest.TestCase):
    def setUp(self):
        fixture=old.Tasks("test_complete_then_duplicate_is_read_only");fixture.setUp()
        self.fixture=fixture

    def test_alias_is_resolved_to_actual_bound_client_identity(self):
        root=self.fixture.root
        bindings={"denied":dict(self.fixture.binding,client_id="beta")}
        name=store.task_name("beta","unauth")
        store.create(root,name,engine.specification(bindings["denied"],"read current"))
        exported,value=f.export_task(root,bindings["denied"],"unauth")
        self.assertEqual(exported["meta"]["task_id"],name)
        self.assertEqual(value["state"],"READY")
        self.assertNotEqual(name,store.task_name("denied","unauth"))

    def test_tripwire_blocks_network_before_connect_and_tools_before_execute(self):
        for kind in ("network","tool"):
            marker=self.fixture.directory/(kind+"-forbidden.json")
            with error_noexec.forbid(marker),self.assertRaises(RuntimeError):
                if kind=="network":
                    with socket.socket() as sock: sock.connect(("127.0.0.1",1))
                else: engine.ProjectRecords(self.fixture.project).execute(None)
            self.assertTrue(marker.exists())
            self.assertEqual(self.fixture.count(),0)
