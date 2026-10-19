"""Offline compare instruction bodies of the two observed loaded kernels."""
import json
from pathlib import Path
import re
import sys
from run_trace import sha


def main():
    root = Path(sys.argv[1])
    bodies = []
    for index in (23, 24):
        text = (root/f'module-{index}.sass').read_text()
        if 'code for sm_110' not in text or text.count('Function :') != 1:
            raise ValueError('expected one actual SM110 function')
        bodies.append(re.findall(r'/\*([0-9a-f]+)\*/\s+(.*?)\s*;\s*/\*', text))
    prefix = next((i for i, pair in enumerate(zip(*bodies)) if pair[0] != pair[1]),
                  min(map(len, bodies)))
    tensor = [[(i, address, instruction) for i, (address, instruction) in enumerate(body)
               if 'HMMA.' in instruction] for body in bodies]
    if tensor[0] != tensor[1] or len(tensor[0]) != 8:
        raise ValueError('tensor instruction mismatch')
    if any(i >= prefix for i, _, _ in tensor[0]):
        raise ValueError('tensor operations outside identical prefix')
    # Loop back edge must also remain inside the common prefix.
    if ('13b0', '@P2 BRA 0xe60') not in bodies[0][:prefix]:
        raise ValueError('expected common loop back edge missing')
    result = dict(status='PASS', instruction_counts=list(map(len, bodies)),
                  identical_prefix_instructions=prefix,
                  first_different_instruction=[body[prefix] for body in bodies],
                  common_tensor_instructions=tensor[0],
                  note='Compares decoded address/opcode/operands, not scheduling bits; '
                       'same parameters and independently reproduced outputs required separately.',
                  files={f'module-{i}.sass':sha(root/f'module-{i}.sass') for i in (23,24)})
    (root/'sass-audit.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
