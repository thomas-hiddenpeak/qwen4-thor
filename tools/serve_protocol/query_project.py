"""Real project query using the pinned JS SDK and Python durable tool executor.

This makes actual local model requests: validation must use the shared GPU
lease. It never starts a model server or replays a failed journal.
"""
import argparse
from pathlib import Path

from js_client import JsSdkClient
from project_query import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--sdk-directory", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--protocol-sha256")
    parser.add_argument("--max-rounds", type=int, default=4)
    args = parser.parse_args()
    client = JsSdkClient(args.base_url, args.sdk_directory, protocol_sha256=args.protocol_sha256)
    print(run(args.directory, args.base_url, args.question, max_rounds=args.max_rounds, client=client))


if __name__ == "__main__":
    main()
