"""Host adapter/schema tests; no hardware codec or model execution."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from jsonschema import Draft202012Validator
from tools.examples import vision_input as v
from tools.serve_protocol.build_contract import vision_schema


class Inputs(unittest.TestCase):
    def test_bounded_file_and_format(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "image.png"
            p.write_bytes(b"not image")
            with self.assertRaises(ValueError): v.image_part(p)
            Image.new("RGB", (32, 32), "red").save(p)
            item = v.image_part(p)
            self.assertEqual(item["image_url"]["detail"], "auto")
            self.assertEqual(base64.b64decode(item["image_url"]["url"].split(",")[1]),
                             p.read_bytes())
            with patch.object(v, "MAX_PAYLOAD_BYTES", 8):
                with self.assertRaises(ValueError): v.image_part(p)

    def test_video_limits_before_worker(self):
        with tempfile.NamedTemporaryFile() as source, patch.object(v.subprocess, "run") as run:
            for values in ({"frames":1}, {"frames":33}, {"frames":True},
                           {"timeout":float("nan")}, {"timeout":61}, {"backend":"auto"}):
                with self.assertRaises(ValueError): v.sample_video(source.name, **values)
            run.assert_not_called()

    def test_real_lossless_video_pts_and_pixels(self):
        fixture = Path(os.environ["Q4T_VISION_FIXTURES"])
        part = v.sample_video(fixture / "motion.mov", frames=8)
        self.assertEqual(part["video_timestamps"], [i*.5 for i in range(8)])
        for index, url in enumerate(part["video_frames"]):
            actual = Image.open(io.BytesIO(base64.b64decode(url.split(",")[1]))).convert("RGB")
            expected = Image.open(fixture / f"motion-{index:02d}.png").convert("RGB")
            self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_schema_roles_controls_and_timestamps(self):
        schema = vision_schema()
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        part = {"type":"video_frames", "video_frames":["x","y"],
                "video_timestamps":[0,.5]}
        value = {"messages":[{"role":"user", "content":[part]}],
                 "response_format":{"type":"json_object"}}
        self.assertTrue(validator.is_valid(value))
        for bad in ({"video_timestamps":[-1,.5]}, {"video_frames":["x"]},
                    {"type":"video_url"}, {"fps":24}):
            copied = json.loads(json.dumps(value)); copied["messages"][0]["content"][0].update(bad)
            self.assertFalse(validator.is_valid(copied))
        for role in ("assistant", "tool", "system"):
            copied = json.loads(json.dumps(value)); copied["messages"][0]["role"] = role
            self.assertFalse(validator.is_valid(copied))


if __name__ == "__main__": unittest.main()
