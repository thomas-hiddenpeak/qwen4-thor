"""Prepare fixed synthetic retrieval regressions, not a general quality benchmark."""
import argparse
import hashlib
import json
from pathlib import Path

from transformers import AutoTokenizer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    tok = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
    notes = [
        'The storage service writes a daily operational report.',
        'The scheduler processes pending requests in order.',
        'A benchmark records latency and throughput separately.',
        'Each release is reviewed for correctness and resource usage.',
    ]
    text = ''.join(f'Record {i}: {notes[i % 4]}\n' for i in range(20000))
    # This overfilled preparation buffer is trimmed before any HTTP request.
    filler = tok.encode(text, add_special_tokens=False)
    items = []
    for length in [1024, 4096, 8192, 45056, 204800]:
        for depth in ([0.1, 0.5, 0.9] if length <= 8192 else [0.5]):
            key = f'{length}-{int(depth * 100)}'
            answer = str(710003 + len(items) * 127)
            needle = (f'\nIMPORTANT AUDIT RECORD: The unique audit_code is {answer}. '
                      'This is the authoritative audit_code.\n')
            question = ('\nQuestion: What is the audit_code in the authoritative audit '
                        'record? Return only the six digits, with no explanation.\n')
            budget = length - 150
            for _ in range(16):
                pos = int(budget * depth)
                body = tok.decode(filler[:pos]) + needle + tok.decode(filler[pos:budget]) + question
                messages = [
                    {'role': 'system', 'content': 'Read the document and answer the question exactly. Do not explain your answer.'},
                    {'role': 'user', 'content': body},
                ]
                prompt = tok.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
                actual = len(tok.encode(prompt, add_special_tokens=False))
                if actual == length:
                    break
                budget += length - actual
            else:
                raise RuntimeError(f'{key}: cannot prepare exact length ({actual})')
            if prompt.count(answer) != 1:
                raise RuntimeError(f'{key}: ambiguous answer in prompt')
            items.append({
                'id': key, 'length': length, 'depth': depth, 'expected': answer,
                'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                'body': {'prompt': prompt, 'max_tokens': 32, 'temperature': 0,
                         'stream': True, 'stream_options': {'include_usage': True}},
            })
    (args.output / 'manifest.json').write_text(json.dumps(
        [{k: v for k, v in item.items() if k != 'body'} for item in items], indent=2))
    (args.output / 'requests.jsonl').write_text(''.join(
        json.dumps(item['body']) + '\n' for item in items))
    print(f'Prepared {len(items)} native-template exact-answer requests')


if __name__ == '__main__':
    main()
