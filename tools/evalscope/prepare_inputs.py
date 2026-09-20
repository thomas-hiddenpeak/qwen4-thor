"""Prepare deterministic text workloads; E2E usage verifies their actual size.

These controlled English technical notes are a performance workload, not a
quality benchmark or a substitute for the user's production request corpus.
"""
import argparse
import json
import random
from pathlib import Path

from transformers import AutoTokenizer

parser = argparse.ArgumentParser()
parser.add_argument('--model-dir', required=True)
parser.add_argument('--length', type=int, required=True)
parser.add_argument('--number', type=int, default=3)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
notes = [
    'The service reads model weights and updates the sequence state on each step. '
    'The team records latency and checks the generated answer before accepting a change.',
    'The request contains technical notes from several experiments. '
    'Input length, output length, and concurrency must remain fixed when comparing runs.',
    'The storage worker reads selected rows from the lookup table. '
    'The table is much larger than the small buffer used by one request.',
    'A short request and a long request can follow different execution paths. '
    'The report must distinguish the time to the first answer from the later generation rate.',
    'A cache can reuse recently accessed data. '
    'An estimate of required bytes is different from a measurement of physical memory traffic.',
    'The scheduler tracks each request separately. '
    'It must preserve the correct state when a request finishes or another request arrives.',
    'A useful change can reduce code complexity while keeping the same accuracy and speed. '
    'A faster result on one input does not justify a regression on another input.',
    'The evaluation stores the complete request and response with the build identifier. '
    'The next run uses the same data so that the results remain comparable.',
]
prefix = 'Technical review notes. Read the following records carefully.\n'
suffix = ('\nTask: Write a detailed technical review of these records in at least '
          '500 words. Explain measurement, storage, scheduling, and correctness.\nAnswer:')
with args.output.open('w') as output:
    for index in range(args.number):
        rng = random.Random(20260920 + index)
        records = []
        # Deliberately overfill, then trim by tokenizer IDs during DATA PREPARATION.
        # Server usage, not this local count, is the acceptance authority.
        while len(records) < args.length // 25 + 16:
            records.append(f'Record {len(records) + 1}: {rng.choice(notes)}\n')
        body_ids = tokenizer.encode(''.join(records), add_special_tokens=False)
        budget = args.length - len(tokenizer.encode(prefix + suffix, add_special_tokens=False))
        for _ in range(16):
            prompt = prefix + tokenizer.decode(body_ids[:budget]) + suffix
            count = len(tokenizer.encode(prompt, add_special_tokens=False))
            if count == args.length:
                break
            budget += args.length - count
        else:
            raise SystemExit('Could not prepare the requested local token length')
        output.write(json.dumps({'prompt': prompt}, ensure_ascii=False) + '\n')
