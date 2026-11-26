import copy
import unittest

from tools.serve_chat_storage import parts_profiles as parts
from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import chatkit_actions_profiles as handoffs
from tools.serve_service import package


class StoredPartsHandoff(unittest.TestCase):
    def setUp(self):
        client = package.read(parts.PROOF)["client"]
        saved = {k: client[k] for k in ("chat_id", "cursor", "message_ids", "history_sha256", "peer")}
        self.first = dict(saved=saved, restored=None, sdk=client["sdk"], reply=client["reply"],
                          finish="stop", independent_chat=client["independent_chat"])
        self.second = copy.deepcopy(self.first)
        ids = ["chatmsg_"+c*64 for c in "bcd"]
        self.second.update(saved=dict(saved, chat_id="chatcmpl-auto-test", message_ids=ids,
                                      cursor=ids[1], history_sha256="f"*64),
                           restored=copy.deepcopy(saved))

    def test_preserves_reviewed_predecessor_and_original_page(self):
        parts.check(self.second, self.first)
        spec = handoffs.handoff({parts.KEY: {}})
        self.assertEqual(spec.predecessor, reuse.NULL_HISTORY_RECEIPT)
        self.assertEqual(spec.reviewed, reuse.NULL_HISTORY_REVIEWED)
        self.assertEqual(reuse.ACCEPTANCE_LABELS[reuse.PARTS_RECORD], "ACCEPT_STORED_CONTENT_PARTS_PRIMARY_LIGHT")

    def test_rejects_original_id_digest_cursor_and_owner_changes(self):
        saved = self.second["saved"]
        for key in saved:
            row = copy.deepcopy(self.second)
            row["restored"][key] = 200 if key == "peer" else saved[key]
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                parts.check(row, self.first)

    def test_requires_fresh_page_and_real_answers(self):
        for field, value in (("reply","blue"), ("finish","length"), ("independent_chat","42")):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                parts.check(dict(self.second, **{field: value}), self.first)
        for field in ("chat_id", "message_ids", "history_sha256"):
            saved = dict(self.second["saved"], **{field: self.first["saved"][field]})
            if field == "message_ids":
                saved["cursor"] = saved[field][1]
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                parts.check(dict(self.second, saved=saved), self.first)

    def test_rejects_numeric_coercion_and_duplicate_ids(self):
        saved = self.second["saved"]
        for field, value in (("peer",404.0), ("cursor",saved["message_ids"][0]),
                             ("message_ids",[saved["cursor"]]*3)):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                parts.check(dict(self.second, saved=dict(saved, **{field:value})), self.first)
