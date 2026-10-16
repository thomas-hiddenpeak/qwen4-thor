"""CPU-only live child checks: no CUDA, server, or HTTP requests."""
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from server_address_space import (ADDR_NO_RANDOMIZE, launch_command,
                                  personality, snapshot, require_mapping_match)


class AddressSpaceTests(unittest.TestCase):
    def test_default_command_unchanged(self):
        cmd = ['/bin/true', '--literal']
        self.assertEqual(launch_command(cmd, None), cmd)
        self.assertEqual(launch_command(cmd, 'inherit'), cmd)
        self.assertEqual(cmd, ['/bin/true', '--literal'])

    def test_no_silent_fallback(self):
        with patch('server_address_space.shutil.which', return_value=None):
            with self.assertRaises(RuntimeError):
                launch_command(['/bin/true'], 'no-aslr')
        with self.assertRaises(ValueError):
            launch_command(['/bin/true'], 'bad')

    def child(self, mode):
        before = personality(os.getpid())
        binary = Path('/bin/sleep').resolve()
        # Explicit exec avoids racing setarch's pre-exec image: wait until
        # /proc/exe is sleep, using a bounded CPU-only readiness check.
        p = subprocess.Popen(launch_command([str(binary), '10'], mode))
        try:
            import time
            for _ in range(100):
                if p.poll() is not None:
                    self.fail(f'child failed with {p.returncode}')
                if Path(f'/proc/{p.pid}/exe').resolve() == binary:
                    break
                time.sleep(.01)
            else:
                self.fail('child exec timeout')
            info, raw = snapshot(p.pid, binary, mode, before)
            self.assertTrue(raw)
            self.assertEqual(personality(os.getpid()), before)
            with self.assertRaises(RuntimeError):
                snapshot(p.pid, '/bin/false', mode, before)
            return info
        finally:
            p.terminate()
            p.wait(timeout=5)

    def test_only_child_changes_and_fixed_code_maps_repeat(self):
        parent = personality(os.getpid())
        a = self.child('no-aslr')
        b = self.child('no-aslr')
        client = self.child('inherit')
        self.assertTrue(a['aslr_disabled'])
        self.assertTrue(b['aslr_disabled'])
        self.assertEqual(a['executable_mappings'], b['executable_mappings'])
        self.assertEqual(client['aslr_disabled'], bool(parent & ADDR_NO_RANDOMIZE))
        self.assertEqual(personality(os.getpid()), parent)

    def test_mapping_reference_rejects_shifted_or_uncontrolled_code(self):
        current = {'mode': 'no-aslr', 'aslr_disabled': True, 'exe': '/fake/q4t',
                   'executable_mappings': [{'range': '1000-2000'}]}
        require_mapping_match(current, dict(current))
        for change in [{'mode': 'inherit'}, {'aslr_disabled': False},
                       {'exe': '/other/q4t'},
                       {'executable_mappings': [{'range': '2000-3000'}]}]:
            with self.assertRaises(RuntimeError):
                require_mapping_match(current, dict(current, **change))

    def test_wrong_personality_rejected(self):
        with patch('server_address_space.personality', return_value=0), \
             patch('server_address_space.start_ticks', return_value='1'), \
             patch('server_address_space.os.readlink', return_value='/bin/sleep'), \
             patch('pathlib.Path.read_text', return_value=''):
            with self.assertRaisesRegex(RuntimeError, 'ASLR personality'):
                snapshot(1, '/bin/sleep', 'no-aslr', 0)


if __name__ == '__main__':
    unittest.main()
