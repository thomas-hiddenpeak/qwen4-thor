"""Reuse offline unified-RAM and accepted-work accounting on completed phases."""
import argparse
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['a', 'b', 's', 'c', 'h'])
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        'release_resources', ROOT/'tools/prefix_seed/resources.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BASE = ROOT/'.q4t-work/mtp-prefill-release-20261029'
    sys.argv = [sys.argv[0], args.phase]
    module.main()
