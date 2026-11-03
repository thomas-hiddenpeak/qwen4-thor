"""Fresh stdlib-only acceptance helper, then exec the pinned production entry."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_service import package as pkg, supervisor as sup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parent', type=int, required=True)
    args = parser.parse_args()
    sup.arm_parent_death(sup.process_identity(args.parent))
    plan = pkg.read(args.plan)
    package = Path(plan['package']['package'])
    manifest = pkg.verify(package, plan['package']['manifest_sha256'])
    command = [manifest['python']['path'], '-I', '-B',
        str(package/'source/tools/serve_service/service.py'), 'run',
        '--package', str(package), '--manifest-sha256', plan['package']['manifest_sha256'],
        '--output', str(args.output)]
    environment = {k:v for k,v in os.environ.items() if
        k not in ('LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME') and not k.startswith('Q4T_')}
    os.execve(command[0], command, environment)


if __name__ == '__main__':
    main()
