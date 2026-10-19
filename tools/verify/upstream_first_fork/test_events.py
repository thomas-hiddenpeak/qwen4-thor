"""Host capture-inventory rejection controls; no GPU or model dependency."""
import json
from pathlib import Path
import struct
import tempfile
import unittest

from analyze import PROJECTIONS, validate_events, expected_elements
from report import sha


class EventTest(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parents[3] / '.q4t-work/upstream-event-tests'
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tokens = list(range(76))
        self.events = []
        for p, t in [(72+i, 1) for i in range(4)] + [(72, 4)]:
            for layer in range(3):
                names = ['trunk.in', 'trunk.out', 'ssm.before', 'ssm.after', 'conv.before', 'conv.after']
                projections = PROJECTIONS + (['ple.key', 'ple.value'] if layer == 1 else [])
                names += [name + suffix for name in projections for suffix in ['.x', '.y']]
                if layer == 1:
                    names += ['ple.embeddings', 'ple.conv.before', 'ple.conv.after']
                for name in names:
                    file = f'p{p}-t{t}-l{layer}.{name}'
                    fp32 = name.startswith('ssm')
                    elements = expected_elements(name, t)
                    data = bytes(elements * (4 if fp32 else 2))
                    (self.root/file).write_bytes(data)
                    self.events.append(dict(index=len(self.events), file=file,
                        elements=elements, dtype='f32' if fp32 else 'bf16', layer=layer,
                        position=p, rows=t, sha256=sha(data),
                        prefix_sha256=sha(struct.pack('<'+'i'*(p+t),*self.tokens[:p+t]))))

    def check(self):
        (self.root/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in self.events))
        return validate_events(self.root, self.tokens)

    def test_inventory(self):
        self.assertEqual(len(self.check()), 425)

    def test_missing(self):
        self.events.pop()
        with self.assertRaisesRegex(ValueError, 'inventory'):
            self.check()

    def test_duplicate(self):
        self.events[1] = dict(self.events[0], index=1)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.check()

    def test_prefix(self):
        self.events[0]['prefix_sha256'] = sha(b'wrong')
        with self.assertRaisesRegex(ValueError, 'prefix'):
            self.check()

    def test_nonfinite_with_valid_checksum(self):
        data = struct.pack('<H', 0x7fc0) + bytes(self.events[0]['elements'] * 2 - 2)
        (self.root/self.events[0]['file']).write_bytes(data)
        self.events[0]['sha256'] = sha(data)
        with self.assertRaisesRegex(ValueError, 'nonfinite'):
            self.check()


if __name__ == '__main__':
    unittest.main()
