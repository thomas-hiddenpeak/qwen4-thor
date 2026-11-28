"""Run the Assistants CPU fixture with a local SDK path map and a release receipt.

Invoke as python -m tools.serve_audit.assistant_crud_cpu SDK_MAP RECEIPT OUTPUT.
Set Q4T_FILES_HOST to the native test binary and Q4T_FILES_INGRESS to q4t-ingress.
"""
import argparse
import json
from pathlib import Path

from tools.serve_audit.assistant_crud_business import exercise
from tools.serve_recovery.test_native_access import certificates
from tools.serve_responses.test_files_transport import Stack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('sdk_map', 'receipt', 'output'):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(mode=0o700)
    cert = args.output / 'cert'
    certificates(cert)
    stack = Stack(args.output/'stack', cert, json_limit=16384, assistant_fixture=True)
    try:
        result = exercise(json.loads(args.sdk_map.read_bytes()), args.receipt.read_bytes(),
                          base=f'https://127.0.0.1:{stack.port}/v1', ca=cert/'ca.pem',
                          owner=stack.key, other=stack.other_key, output=args.output)
    finally:
        stack.close()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
