"""Direct acceptance-oracle checks before spending a bounded model lease."""
import json
import unittest

from tools.serve_responses import batches_acceptance as stage


def usage(inputs=10, outputs=3):
    return dict(input_tokens=inputs, output_tokens=outputs, total_tokens=inputs+outputs,
        input_tokens_details=dict(cached_tokens=0), output_tokens_details=dict(reasoning_tokens=0))


def batch():
    return dict(id='batch_test', object='batch', endpoint='/v1/chat/completions',
        input_file_id='file_test', completion_window='24h', created_at=1,
        status='completed', request_counts=dict(total=2, completed=1, failed=1),
        usage=usage())


class BatchAcceptanceTests(unittest.TestCase):
    def test_frozen_generation_budget_and_finite_rows(self):
        rows = stage.requests()
        count = sum(len(rows[key]) for key in ('chat','responses','completions','cancel','rollback'))+1
        self.assertEqual(count,8)
        self.assertEqual(stage.LIMITS,dict(starts=2,chats=8,controls=2000,seconds=1200))
        self.assertEqual(stage.MAX_MODEL_STEPS,10)
        self.assertTrue(rows['follow']['stream'])
        self.assertTrue(rows['responses'][0]['background'])
        for key in ('chat','responses','completions','cancel','rollback'):
            for row in rows[key]:self.assertFalse(row.get('stream',False))
        self.assertEqual(rows['chat'][1]['max_completion_tokens'],1)
        self.assertEqual(rows['cancel'][0]['max_output_tokens'],1536)

    def test_jsonl_rejects_truncated_reordered_or_missing_receipts(self):
        rows=[dict(id='req_'+str(i),custom_id='item-'+str(i),error=None,
                   response=dict(status_code=200,body=dict(answer=42))) for i in range(2)]
        encode=lambda value:(''.join(json.dumps(row)+'\n' for row in value)).encode()
        self.assertEqual(stage.jsonl(encode(rows),['item-0','item-1']),rows)
        for raw in [encode(rows)[:-1],encode(rows[::-1]),encode(rows+rows[:1]),
                    encode([dict(rows[0],response=None),rows[1]])]:
            with self.assertRaises(RuntimeError):stage.jsonl(raw,['item-0','item-1'])

    def test_actual_usage_rejects_unknown_invalid_or_reserved_substitutes(self):
        self.assertEqual(stage.usage(usage()),usage())
        for value in [None,dict(usage(),total_tokens=99),dict(usage(),output_tokens=-1),
                      dict(usage(),input_tokens=True),dict(usage(),
                           input_tokens_details=dict(cached_tokens=11))]:
            with self.assertRaises(RuntimeError):stage.usage(value)
        value=batch()
        success=dict(usage=dict(prompt_tokens=4,completion_tokens=2))
        stage.check_failure_usage(value,success)
        for invalid in [dict(value,usage=usage(4,3)),dict(value,usage=usage(10,2)),
                        dict(value,usage=usage(10,1536)),dict(value,usage=None)]:
            with self.assertRaises(RuntimeError):stage.check_failure_usage(invalid,success)

    def test_terminal_and_counts_cannot_hide_unfinished_or_failed_rows(self):
        value=batch()
        self.assertEqual(stage.check_batch(value,2,1,1),value['usage'])
        for bad in [dict(value,status='finalizing'),dict(value,status='cancelled'),
                    dict(value,request_counts=dict(total=2,completed=2,failed=0))]:
            with self.assertRaises(RuntimeError):stage.check_batch(bad,2,1,1)
        cancelled=dict(value,status='cancelled',request_counts=dict(total=1,completed=1,failed=0))
        stage.check_batch(cancelled,1,1,0,'cancelled')


if __name__=='__main__':unittest.main()
