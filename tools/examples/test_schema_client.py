"""Public CPU contracts for explicit structured answers; no media/model files."""
from copy import deepcopy
import json
import unittest

import chat_client as c


SCHEMA = dict(type="object", properties=dict(direction=dict(type="string",
    enum=["left_to_right", "right_to_left"], description="箭头向右 means left_to_right")),
    required=["direction"], additionalProperties=False)


class SchemaClient(unittest.TestCase):
    def body(self, content="Which direction?"):
        return c.make_request([dict(role="user", content=content)], [], stream=False)

    def test_text_builder_includes_semantics_and_same_constraint(self):
        value = c.make_request([dict(role="user", content="Which direction?")], [],
                               json_schema=SCHEMA, max_tokens=768)
        self.assertEqual(value["max_tokens"], 768)
        prompt = value["messages"][0]["content"].split(c.SCHEMA_INSTRUCTION)[1]
        self.assertEqual(json.loads(prompt), SCHEMA)
        self.assertEqual(value["response_format"]["json_schema"]["schema"], SCHEMA)

    def test_media_and_caller_ownership(self):
        original = self.body([dict(type="image_url", image_url="data:image/png;base64,AA=="),
                              dict(type="text", text="Which direction?")])
        before, schema = deepcopy(original), deepcopy(SCHEMA)
        result = c.with_json_schema(original, schema)
        schema["properties"].clear()
        self.assertEqual(original, before)
        self.assertEqual(result["messages"][0]["content"][:-1], before["messages"][0]["content"])
        self.assertEqual(result["response_format"]["json_schema"]["schema"], SCHEMA)

    def test_final_tool_turn_preserves_actual_tool_messages(self):
        body = self.body()
        body["messages"] += [dict(role="assistant", content=None, tool_calls=[dict(
            id="call-x", type="function", function=dict(name="read", arguments='{}'))]),
            dict(role="tool", tool_call_id="call-x", content='{"actual":123}')]
        body.update(tools=[dict(type="function", function=dict(name="read"))], tool_choice="none")
        result = c.with_json_schema(body, SCHEMA)
        self.assertEqual(result["messages"][1:], body["messages"][1:])
        self.assertEqual(result["tools"], body["tools"])
        self.assertIn("箭头向右", result["messages"][0]["content"])

    def test_invalid_combinations_and_history_fail_before_network(self):
        body = self.body()
        for changed in (dict(body, response_format={}), dict(body, parse_response=False),
                        dict(body, session_id="a"*64), dict(body, prefix_cache_scope="x"),
                        dict(body, tools=[{}]), dict(body, messages=[]),
                        dict(body, messages=[dict(role="tool", content="x")]),
                        dict(body, messages=[dict(role="user", content=None)])):
            with self.subTest(body=changed), self.assertRaises(c.ProtocolError):
                c.with_json_schema(changed, SCHEMA)

    def test_lossy_json_and_nonfinite_values_rejected(self):
        for value in (float("nan"), float("inf"), 9007199254740992, 10**400,
                      {1: "lossy"}, (1, 2), "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(c.ProtocolError):
                c.with_json_schema(self.body(), dict(type="object", description=value))

    def test_utf8_byte_budget_and_schema_name(self):
        small = dict(type="object", description="中文" * 10900)
        c.with_json_schema(self.body(), small)
        for schema in (dict(type="array"), dict(type="object", description="中文"*11000)):
            with self.assertRaises(c.ProtocolError): c.with_json_schema(self.body(), schema)
        for name in ("", "space name", "x"*65, "答案"):
            with self.assertRaises(c.ProtocolError): c.with_json_schema(self.body(), SCHEMA, name=name)

    def test_output_budget_bounds_and_existing_defaults(self):
        self.assertEqual(self.body()["max_tokens"], 512)
        self.assertNotIn("response_format", self.body())
        for budget in (0, -1, True, 1.5, None, 2147483648):
            with self.subTest(budget=budget), self.assertRaises(c.ProtocolError):
                c.make_request([dict(role="user", content="q")], [], max_tokens=budget)


if __name__ == "__main__": unittest.main()
