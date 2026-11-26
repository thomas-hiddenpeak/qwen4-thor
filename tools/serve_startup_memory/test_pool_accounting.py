import unittest

from tools.serve_startup_memory import pool_accounting as pool


def observations(mode='async'):
    result = {}
    threshold = pool.PAYLOAD_BYTES if mode == 'async-retained' else 0
    for index, (phase,touch) in enumerate(zip(
            pool.PHASES, ('context_initialized','gpu_all_words_verified_edges',
                          'release_returned_success','pool_trim_returned_success'))):
        used = pool.PAYLOAD_BYTES if phase == 'held' else 0
        reserved = max(used, threshold if phase == 'freed' else 0)
        native = dict(kind=mode,phase=phase,touch=touch,bytes=pool.PAYLOAD_BYTES,
                      cuda_free_bytes=1024**3-reserved,cuda_total_bytes=1024**3,
                      pool_release_threshold_bytes=threshold,pool_used_bytes=used,
                      pool_reserved_bytes=reserved)
        samples = [dict(rss_bytes=8192,current_bytes=16384,
                        monotonic_ns=2*index+i+1,events=dict(high=0,max=0,oom=0,oom_kill=0))
                   for i in range(2)]
        result[phase] = dict(native=native,samples=samples)
    return result


class PoolAccountingTest(unittest.TestCase):
    def test_synchronized_release_can_leave_backing_in_pool(self):
        d = pool.summarize('async-retained', observations('async-retained'))
        self.assertEqual(d['freed']['pool_used_bytes'],0)
        self.assertEqual(d['freed']['pool_reserved_bytes'],pool.PAYLOAD_BYTES)
        self.assertEqual(d['freed']['current_bytes'],[0,0])
        self.assertEqual(d['trimmed']['pool_reserved_bytes'],0)

    def test_preserve_host_and_pool_deltas_as_independent_values(self):
        data = observations()
        data['held']['samples'][0]['current_bytes'] = 8192
        result = pool.summarize('async',data)
        self.assertEqual(result['held']['current_bytes'],[-8192,0])
        self.assertEqual(result['held']['pool_used_bytes'],pool.PAYLOAD_BYTES)

    def test_stale_sample_and_changed_pressure_are_detected(self):
        data = observations()
        data['held']['samples'][0]['monotonic_ns'] = 2
        with self.assertRaises(ValueError):
            pool.summarize('async',data)
        data = observations()
        data['held']['samples'][1]['events']['oom_kill'] = 1
        with self.assertRaises(ValueError):
            pool.summarize('async',data)

    def test_incomplete_gpu_work_and_invalid_pool_counters_are_detected(self):
        for field,value in [('touch','host_only'),('pool_reserved_bytes',0),
                            ('pool_used_bytes',True),('bytes',1)]:
            with self.subTest(field=field):
                data = observations()
                data['held']['native'][field] = value
                with self.assertRaises(ValueError):
                    pool.summarize('async',data)

    def test_release_must_finish_and_all_phases_must_be_present(self):
        data = observations('async-retained')
        data['freed']['native']['pool_used_bytes'] = pool.PAYLOAD_BYTES
        with self.assertRaises(ValueError):
            pool.summarize('async-retained',data)
        del data['trimmed']
        with self.assertRaises(ValueError):
            pool.summarize('async-retained',data)


if __name__ == '__main__':
    unittest.main()
