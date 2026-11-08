"""Direct driver negative checks, no local model or GPU."""
import copy
import unittest

from tools.serve_chat_storage import acceptance as stage


class Driver(unittest.TestCase):
    def test_stream_oracle_rejects_lost_choices_probabilities_identity_and_usage(self):
        log = dict(token='42', logprob=-0.25, bytes=[52, 50], top_logprobs=[
            dict(token='42', logprob=-0.25, bytes=[52, 50]),
            dict(token='41', logprob=-2, bytes=[52, 49])])
        usage = dict(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        stored = dict(object='chat.completion', id='chat_1', created=1, model='q4t',
            choices=[dict(index=i, finish_reason='stop',
                message=dict(role='assistant', content='42'),
                logprobs=dict(content=[log], refusal=None)) for i in range(2)], usage=usage)
        base = dict(object='chat.completion.chunk', id='chat_1', created=1, model='q4t')
        chunks = []
        for i in range(2):
            chunks.append(dict(base, choices=[dict(index=i, delta=dict(role='assistant', content='42'),
                finish_reason=None, logprobs=dict(content=[log], refusal=None))], usage=None))
            chunks.append(dict(base, choices=[dict(index=i, delta={}, finish_reason='stop', logprobs=None)], usage=None))
        chunks.append(dict(base, choices=[], usage=usage))
        stage.check_stream(chunks, stored)
        dropped = copy.deepcopy(chunks)
        dropped[2]['choices'][0]['logprobs']['content'] = []
        changed_id = copy.deepcopy(chunks)
        changed_id[0]['id'] = 'chat_other'
        for value in (chunks[:-1], chunks[:2]+chunks[-1:], dropped, changed_id,
                      chunks+chunks[-1:], chunks[:1]+chunks[2:]):
            with self.assertRaises(RuntimeError):
                stage.check_stream(value, stored)

    def test_final_storage_failure_never_claims_success(self):
        prefix = dict(choices=[dict(index=0, delta=dict(content='1,'), finish_reason=None)], usage=None)
        error = dict(error=dict(code='object_commit_unknown'))
        events = [prefix, error]
        raw = b'data: prefix\n\ndata: error\n\ndata: [DONE]\n\n'
        stage.check_fault(events, raw)
        finished = copy.deepcopy(prefix)
        finished['choices'][0]['finish_reason'] = 'stop'
        used = dict(prefix, usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        for value in ([finished, error], [used, error], [error], events+events[:1], events[:-1]):
            with self.assertRaises(RuntimeError):
                stage.check_fault(value, raw)
        with self.assertRaises(RuntimeError):
            stage.check_fault(events, raw+raw)


if __name__ == '__main__':
    unittest.main()
