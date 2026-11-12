"""Real CPU media preparation for the exact bounded business combination."""
import copy
import json
import os
from pathlib import Path
import subprocess
import unittest

from tools.serve_schema_v2 import media


class MediaPreparation(unittest.TestCase):
    def test_real_fixture_and_followup_use_production_media_preflight(self):
        files = media.fixtures()
        self.assertEqual(len(files), 5)
        rows = media.requests()
        follow = copy.deepcopy(rows['tool'])
        follow['tool_choice'] = 'auto'
        # CPU-only history planning fixture, not a generated call or GPU proof.
        follow['messages'] += [dict(role='assistant', content=None, tool_calls=[
            dict(id='call_cpu_media', type='function', function=dict(
                name='read_project_record', arguments=json.dumps(dict(
                    path='docs/MODEL.md', start_line=1, line_count=16))))]),
            dict(role='tool', tool_call_id='call_cpu_media',
                 content='{"ok":true,"source":{"citation":"[docs/MODEL.md:L1-L16]"}}')]
        rows['final'] = follow
        model = Path.home() / 'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
        data = ''.join(json.dumps(dict(allow_media=True, max_len=16384,
            max_prefill=8192, body=dict(request=row))) + '\n' for row in rows.values())
        result = subprocess.run([os.environ['Q4T_MEDIA_PREFLIGHT_PROBE'],
            str(model / 'tokenizer.json')], input=data, text=True,
            capture_output=True, check=True, timeout=60)
        if os.environ.get('Q4T_MEDIA_CPU_OUTPUT'):
            out = Path(os.environ['Q4T_MEDIA_CPU_OUTPUT'])
            (out / 'preflight.stdout').write_text(result.stdout)
            (out / 'preflight.stderr').write_text(result.stderr)
        answers = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(answers), 4)
        for answer in answers:
            self.assertNotIn('error', answer)
            self.assertGreater(answer['prompt_tokens'], 100)
            self.assertLessEqual(answer['prompt_tokens'], 8192)


if __name__ == '__main__':
    unittest.main()
