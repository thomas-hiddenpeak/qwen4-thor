"""Public CPU contracts for explicit structured answers; no media/model files."""
from copy import deepcopy
from decimal import Decimal
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
        for value in (float("nan"), float("inf"), Decimal("NaN"),
                      {1: "lossy"}, (1, 2), "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(c.ProtocolError):
                c.with_json_schema(self.body(), dict(type="object", description=value))

    def test_utf8_byte_budget_and_schema_name(self):
        small = dict(type="object", description="中" * 699000)
        c.with_json_schema(self.body(), small)
        for schema in (dict(type="array"), dict(type="object", description="中"*699051)):
            with self.assertRaises(c.ProtocolError): c.with_json_schema(self.body(), schema)
        for name in ("", "space name", "x"*65, "答案"):
            with self.assertRaises(c.ProtocolError): c.with_json_schema(self.body(), SCHEMA, name=name)

    def test_exact_numbers_survive_schema_prompt_and_actual_request_encoding(self):
        schema = dict(type="object", properties=dict(n=dict(type="number", enum=[
            Decimal("1e-400"), Decimal("0.30000000000000001"), 10**400,
            9007199254740993])), required=["n"], additionalProperties=False)
        original = deepcopy(schema)
        result = c.with_json_schema(self.body(), schema)
        self.assertEqual(schema, original)
        wire_schema = result["response_format"]["json_schema"]["schema"]
        self.assertEqual(wire_schema, schema)
        api = c.ChatClient("http://127.0.0.1:8080")
        api._open = lambda req, timeout: req.data
        raw = api._post("/v1/chat/completions", result, 1)
        decoded = c.load_json(raw, exact_numbers=True)
        self.assertEqual(decoded["response_format"]["json_schema"]["schema"], schema)
        self.assertIn(b'0.30000000000000001', raw)
        self.assertIn(b'1E-400', raw)
        self.assertNotIn(b'"1E-400"', raw)
        # Native int conversion limits do not truncate long schema integers.
        huge = 10**5000
        self.assertEqual(c.load_json(c.dump_json(huge), exact_numbers=True), huge)

    def test_active_tools_root_ref_and_no_user_are_valid_inputs(self):
        root = {"$ref": "#/$defs/root", "$defs": {"root": SCHEMA}}
        body = self.body()
        body.update(tools=[dict(type="function", function=dict(name="read"))],
                    tool_choice="required")
        result = c.with_json_schema(body, root)
        self.assertEqual(result["tool_choice"], "required")
        self.assertEqual(result["tools"], body["tools"])
        self.assertEqual(result["response_format"]["json_schema"]["schema"], root)
        no_user = dict(body, messages=[dict(role="system", content="Answer briefly")])
        result = c.with_json_schema(no_user, SCHEMA)
        self.assertEqual(result["messages"][1:], no_user["messages"])
        self.assertIn(c.SCHEMA_INSTRUCTION, result["messages"][0]["content"])

    def test_unrepresentable_decimal_exponent_is_protocol_error(self):
        with self.assertRaises(c.ProtocolError):
            c.load_json('{"n":1e' + '9' * 100 + '}', exact_numbers=True)

    def test_output_budget_bounds_and_existing_defaults(self):
        self.assertEqual(self.body()["max_tokens"], 512)
        self.assertNotIn("response_format", self.body())
        for budget in (0, -1, True, 1.5, None, 2147483648):
            with self.subTest(budget=budget), self.assertRaises(c.ProtocolError):
                c.make_request([dict(role="user", content="q")], [], max_tokens=budget)


if __name__ == "__main__": unittest.main()
