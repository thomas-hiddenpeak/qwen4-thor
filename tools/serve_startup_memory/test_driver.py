"""Pure CPU validation of failure classification and accounting comparisons."""
import copy
import unittest

from tools.serve_startup_memory import accounting, verify


class Contracts(unittest.TestCase):
    def records(self):
        rows=[]
        for point in verify.POINTS:
            estimate=100 if point=='before_allocations' else None
            rows.append(dict(checkpoint=point,admission=dict(state='admitted',
                estimate_bytes=estimate,headroom_bytes=1024**2,
                required_bytes=1024**2+(estimate or 0),
                observed_available_bytes=2*1024**2,observed_total_bytes=4*1024**2)))
        return rows

    def test_actual_decision_not_just_success_code(self):
        rows=self.records()
        self.assertEqual(len(verify.validate_records(rows,1)),3)
        rows[1]['admission']['observed_available_bytes']=1
        with self.assertRaises(RuntimeError):verify.validate_records(rows,1)

    def test_negative_must_be_last_expected_checkpoint(self):
        rows=self.records()[:2]
        rows[-1]['admission'].update(state='startup_memory_insufficient',observed_available_bytes=1)
        verify.validate_records(rows,1,'before_allocations')
        with self.assertRaises(RuntimeError):verify.validate_records(rows,1,'before_metadata')

    def test_unknown_and_missing_estimate_rejected(self):
        for field,value in (('observed_available_bytes',None),('estimate_bytes',None),('observed_total_bytes',0)):
            rows=self.records();rows[1]['admission'][field]=value
            with self.assertRaises(RuntimeError):verify.validate_records(rows,1)

    def test_probe_touches_and_release_required(self):
        row=dict(kind='managed',phase='held',bytes=accounting.BYTES,
            cuda_free_bytes=1,cuda_total_bytes=2,touch='gpu_all_words_verified_edges')
        accounting.receipt(row,'managed','held')
        for key,value in (('touch','context_initialized'),('bytes',1),('cuda_free_bytes',3)):
            wrong=dict(row);wrong[key]=value
            with self.assertRaises(RuntimeError):accounting.receipt(wrong,'managed','held')

    def test_separate_counter_deltas_do_not_claim_coverage(self):
        def sample(amount):
            return dict(parent=dict(VmRSS=amount),memory=dict(MemAvailable=1000-amount),
                resources=dict(cgroup=dict(observations={'memory.current':dict(value=amount),
                    'memory.stat':dict(value=dict(anon=amount,file=0))})))
        phases={name:dict(native=dict(cuda_free_bytes=1000-amount),
            observations=[sample(amount),sample(amount)]) for name,amount in
            (('baseline',10),('held',110),('freed',12))}
        result=accounting.summarize(phases)
        self.assertEqual(result['held']['delta_from_baseline'][0]['cgroup_current'],100)
        self.assertEqual(result['held']['delta_from_baseline'][0]['cuda_free'],-100)
        self.assertEqual(result['freed']['delta_from_baseline'][0]['parent_rss'],2)
        self.assertNotIn('total_physical_ram',result)
        phases['held']['observations'].pop()
        with self.assertRaises(RuntimeError):accounting.summarize(phases)

    def test_receipts_do_not_accept_extra_or_reordered_checkpoints(self):
        rows=self.records()
        for invalid in (rows+[copy.deepcopy(rows[-1])],list(reversed(rows)),rows[1:]):
            with self.assertRaises(RuntimeError):verify.validate_records(invalid,1)


if __name__=='__main__':unittest.main()
