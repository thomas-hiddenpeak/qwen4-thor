"""No sockets/models: refuse incomplete or misattributed startup evidence."""
import unittest
from tools.serve_startup_listeners.stage import verify_probe

class ProbeContracts(unittest.TestCase):
    def test_public_and_operations_exact_role_port_errno(self):
        for label,role,port in (('public-conflict','public',101),('operations-conflict','operations',102)):
            text=f'serve start failed: {role} listener bind failed at 127.0.0.1:{port} (errno 98: Address already in use)'
            verify_probe(label,text,1,101,102)
            for bad in (text.replace('127.0.0.1','0.0.0.0'),text.replace('errno 98','errno 24'),
                        text.replace(str(port),'999')):
                with self.assertRaises(RuntimeError):verify_probe(label,bad,1,101,102)

    def test_late_failure_cannot_count_as_early_conflict(self):
        text='public listener bind failed at 127.0.0.1:101 (errno 98: occupied)'
        for extra in ('tokenizer','[q4t][startup-memory]','serving on port'):
            with self.assertRaises(RuntimeError):verify_probe('public-conflict',text+extra,1,101,102)

    def test_original_reproduction_requires_tokenizer_failure(self):
        verify_probe('baseline-conflict','tokenizer load failed: unavailable',1,101,102)
        with self.assertRaises(RuntimeError):
            verify_probe('baseline-conflict','public listener bind failed',1,101,102)

    def test_exit_status_and_invalid_options_are_distinct(self):
        verify_probe('invalid-options','serve options: operations port equals port',2,101,102)
        for code in (0,1,-9):
            with self.assertRaises(RuntimeError):verify_probe('invalid-options','serve options:',code,101,102)
        with self.assertRaises(RuntimeError):verify_probe('unknown','serve options:',1,101,102)

    def test_tokenizer_cleanup_probe_excludes_model_serving(self):
        verify_probe('tokenizer-failure','tokenizer load failed',1,101,102)
        for extra in ('serving on port','[q4t][startup] model'):
            with self.assertRaises(RuntimeError):verify_probe('tokenizer-failure','tokenizer load failed'+extra,1,101,102)

if __name__=='__main__':unittest.main(verbosity=2)
