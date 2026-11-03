"""Bind an acceptance child to its exact parent before exec; no preexec hook."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_service import supervisor as sup

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--parent', required=True)
parser.add_argument('command', nargs=argparse.REMAINDER)
args = parser.parse_args()
sup.arm_parent_death(json.loads(args.parent))
argv = args.command[1:] if args.command[0] == '--' else args.command
os.execv(argv[0], argv)
