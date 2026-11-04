"""Run the pinned supervisor's known-conflict path, under the caller's lease."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package', type=Path, required=True)
parser.add_argument('--digest', required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--parent', type=int, required=True)
args = parser.parse_args()
# -I and a single package source root select the exact frozen implementation.
sys.path.insert(0, str(args.package / 'source'))
from tools.serve_service import package as pkg, supervisor as sup

os.umask(0o077)
sup.arm_parent_death(sup.process_identity(args.parent))
pkg.verify(args.package, args.digest)
args.output.mkdir(mode=0o700)
code = sup.run(args.package, args.digest, args.output)
pkg.save(args.output / 'probe-result.json', dict(returncode=code,
    package=str(args.package), manifest_sha256=args.digest,
    supervisor_sha256=pkg.sha(sup.__file__), owner=sup.process_identity(os.getpid())))
raise SystemExit(code)
