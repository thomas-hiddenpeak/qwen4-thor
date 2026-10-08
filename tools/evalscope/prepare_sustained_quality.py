"""Prepare the frozen eight prompts once, bound to the candidate and tokenizer.

Data preparation only; no model inference, server, or HTTP requests are made.
"""
import argparse
import json
from pathlib import Path
import shutil

from sustained_quality import (CAPACITY, QUESTIONNAIRE, SAMPLING, file_sha256,
                               model_identity, questionnaire, text_sha256,
                               write_json)


def prepare_prompt(tokenizer, case, filler_ids):
    budget = case['length'] - 512
    for _ in range(64):
        if budget < 0 or budget > len(filler_ids):
            raise ValueError('source material cannot fit the frozen input length')
        parts = ['Background archive notes follow. Only explicitly authoritative '
                 'material is evidence for the final task.\n']
        previous = 0
        for block in case['blocks']:
            offset = int(budget * block['depth'])
            parts.append(tokenizer.decode(filler_ids[previous:offset]))
            parts.append('\n\n' + block['text'] + '\n\n')
            previous = offset
        parts.append(tokenizer.decode(filler_ids[previous:budget]))
        parts.append('\n\nTASK / 任务：\n' + case['question'])
        prompt = tokenizer.apply_chat_template(
            [{'role': 'user', 'content': ''.join(parts)}], tokenize=False,
            add_generation_prompt=True, enable_thinking=False)
        count = len(tokenizer.encode(prompt, add_special_tokens=False))
        if count == case['length']:
            return prompt
        budget += case['length'] - count
    raise ValueError('cannot reach frozen exact input length; do not change the case')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if not any(output.is_relative_to(root / d) for d in ['build', '.q4t-work']):
        parser.error('output must be under build/ or .q4t-work/')
    frozen = questionnaire()
    # Import only for the explicit preparation command, never in pure host tests.
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(args.model_dir), local_files_only=True)
    filler = ('Background note: storage boxes remain on their assigned shelves. '
              'This ordinary note is not an authoritative ledger, service record, '
              'incident finding, function specification, or requested excerpt.\n')
    filler_ids = tokenizer.encode(filler * 10000, add_special_tokens=False)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(QUESTIONNAIRE, output / 'questionnaire.json')
    manifest = []
    with (output / 'requests.jsonl').open('w', encoding='utf-8') as requests:
        for case in frozen['cases']:
            prompt = prepare_prompt(tokenizer, case, filler_ids)
            item = {key: case[key] for key in ['id', 'language', 'category', 'length']}
            item['prompt_sha256'] = text_sha256(prompt)
            manifest.append(item)
            requests.write(json.dumps({'prompt': prompt, 'max_tokens': 512,
                                       'temperature': 0, 'seed': 20260920,
                                       'stream': True,
                                       'stream_options': {'include_usage': True}},
                                      ensure_ascii=False) + '\n')
    write_json(output / 'manifest.json', manifest)
    write_json(output / 'identity.json', {
        'schema_version': 1, 'binary_sha256': file_sha256(args.binary),
        'model': model_identity(args.model_dir), 'capacity': CAPACITY,
        'sampling': SAMPLING, 'questionnaire_sha256': file_sha256(QUESTIONNAIRE),
        'manifest_sha256': file_sha256(output / 'manifest.json'),
        'requests_sha256': file_sha256(output / 'requests.jsonl'),
        'local_tokenizer_counts_are_not_server_usage': True})


if __name__ == '__main__':
    main()
