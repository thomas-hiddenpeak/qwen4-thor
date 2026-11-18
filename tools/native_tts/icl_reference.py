#!/usr/bin/env python3
"""Reuse the pinned Base reference; change only explicit official ICL inputs.

The official generate/generate_icl_prompt math is unchanged. A call adapter
supplies ref_ids and the complete reference codes while the existing Base
reference captures the actual prepared inputs and teacher-forced operands.
This is qualification tooling; it is never imported by the serving process.
"""
import argparse
import json
from pathlib import Path
from unittest import mock

if __package__:
    from . import base_reference as base
    from . import icl_acceptance as contract
else:
    import base_reference as base
    import icl_acceptance as contract


def apply_icl_inputs(kwargs, item, ref_ids, ref_codes):
    original = kwargs['voice_clone_prompt']
    assert original['x_vector_only_mode'] == [True]
    assert original['icl_mode'] == [False] and original['ref_code'] is None
    assert len(original['ref_spk_embedding']) == 1
    assert 'ref_ids' not in kwargs
    assert kwargs['non_streaming_mode'] is item['non_streaming']
    assert kwargs['languages'] == [item['language']]
    return dict(kwargs, ref_ids=[ref_ids], voice_clone_prompt=dict(original,
        x_vector_only_mode=[False], icl_mode=[True], ref_code=[ref_codes]))


def main():
    parser = argparse.ArgumentParser(add_help=False)
    for name in ('assets', 'plan', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args, _ = parser.parse_known_args()
    plan = json.loads(args.plan.read_text())
    contract.validate_plan(plan)
    original_import = base.import_official
    calls = []

    def import_icl(directory):
        module = original_import(directory)
        import torch
        from transformers import Qwen2TokenizerFast
        tokenizer = Qwen2TokenizerFast.from_pretrained(
            args.assets, local_files_only=True)
        original_generate = module.Qwen3TTSForConditionalGeneration.generate

        def generate(holder, **kwargs):
            assert len(calls) < 2
            item = plan['cases'][len(calls)]
            ids = torch.tensor([tokenizer.encode(
                '<|im_start|>assistant\n' + item['reference_text'] +
                '<|im_end|>\n', add_special_tokens=False)], dtype=torch.long)
            codes = torch.tensor(contract.read_reference(item), dtype=torch.long)
            assert ids.ndim == 2 and ids.shape[1] > 5
            assert codes.shape == (30, 16)
            calls.append(dict(reference_codes_sha256=item['reference_codes_sha256'],
                              reference_tokens=int(ids.shape[1] - 5),
                              non_streaming=item['non_streaming']))
            return original_generate(holder, **apply_icl_inputs(
                kwargs, item, ids, codes))

        # Only this process's call boundary is adapted. The original official
        # method still constructs and computes the actual reference prompt.
        module.Qwen3TTSForConditionalGeneration.generate = generate
        return module

    with mock.patch.object(base, 'import_official', import_icl):
        base.main()
    assert len(calls) == 2
    path = args.output / 'result.json'
    result = json.loads(path.read_text())
    result.update(icl_inputs_applied=2, base_reference_reused=True,
                  icl_inputs=calls)
    path.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
