import copy
import hashlib
import unittest
from unittest.mock import patch

from tools.serve_responses import program_fork_profiles as forks


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ProgramForkHandoff(unittest.TestCase):
    def setUp(self):
        self.old = dict(original="resp_original", first="call_original", retry="resp_repeat",
                        parent="call_parent", sdk="3.55.1", branches=[], captures={"initial": digest("original")})
        self.hashes = {"original": self.old["captures"]["initial"]}
        for n in (548, 550):
            self.old["branches"].append(dict(revision=n, pending=f"resp_pending{n}", continuation=f"resp_answer{n}",
                                             call_id="call_p_" + digest(str(n))[:56], child_id=f"fc_{n}"))
            for where, label in (("stored", "pending"), ("answer", "answer")):
                self.hashes[f"{label}_{n}"] = self.old["captures"][f"{where}-{n}"] = digest(f"{where}{n}")
        self.hashes.update({s: digest(s) for s in ("branch", "create", "independent", "current_inputs")})
        self.hashes["current_response"] = self.hashes["create"]
        child = "call_p_" + digest("fresh")[:56]
        self.primary = dict(original="resp_original", follow="resp_answer548", child=child,
            child_item="fc_" + digest(child)[:56], sdk="3.55.1", restored=None,
            result="552:219", independent_chat="720041", peer=404, wire_sha256=self.hashes,
            saved=dict(pending="resp_third", id="resp_primary", inputs_sha256=self.hashes["current_inputs"],
                       response_sha256=self.hashes["current_response"]))
        read = {forks.PROOF: {"client": self.old}, forks.RELEASE: {
                "publication": {"publication_revision": 552}, "declared_methods": 219}}
        self.addCleanup(patch.stopall)
        patch.object(forks.package, "read", side_effect=read.__getitem__).start()

    def test_rejects_substitution_of_issued_branch_or_command_result(self):
        forks.check(self.primary)
        for key in ("original", "pending_548", "answer_550", "current_response"):
            row = copy.deepcopy(self.primary)
            row["wire_sha256"][key] = digest("unrelated")
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                forks.check(row)
        for field, value in (("child", self.old["first"]), ("child_item", "fc_" + "a"*56),
                             ("result", "548:219"), ("peer", 200)):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                forks.check(dict(self.primary, **{field: value}))

    def test_recovery_requires_same_child_and_distinct_response_and_pending_ids(self):
        second = copy.deepcopy(self.primary)
        second["restored"] = self.primary["saved"].copy()
        second["saved"].update(id="resp_light", pending="resp_lightbranch")
        for key in ("response", "inputs"):
            second["wire_sha256"]["restored_" + key] = self.primary["saved"][key + "_sha256"]
        forks.check(second, self.primary)
        for key in ("id", "pending"):
            other = copy.deepcopy(second)
            other["saved"][key] = self.primary["saved"][key]
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                forks.check(other, self.primary)
        other = copy.deepcopy(second)
        other["child"] = "call_p_" + digest("other")[:56]
        other["child_item"] = "fc_" + digest(other["child"])[:56]
        with self.assertRaises(RuntimeError):
            forks.check(other, self.primary)
