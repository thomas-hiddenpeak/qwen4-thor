"""Check or restore the delivered handoff pair; execute needs a fresh GPU lease."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_handoff import stage


def check():
    h = stage.h
    h.verify_seal(stage.PACKAGE)
    delivered = stage.BASE/'correction'
    h.verify_seal(delivered)
    plan = h.read(stage.PACKAGE/'plan.json')
    binding = h.read(delivered/'release/binding.json')
    h.require(plan['root'] == str(ROOT) and binding['old'] == plan['old_hashes'] and
        binding['new']['q4t'] == plan['candidate_sha256'] and
        h.read(delivered/'finalized.json')['status'] == 'DELIVERED', 'foreign pair')
    h.verify_pair(stage.PACKAGE/'previous/q4t', stage.PACKAGE/'previous/q4t.release.json',
                  stage.OLD, stage.PACKAGE/'previous/CMakeCache.txt')
    stage.a.verify_hashes(delivered/'pair', binding['new'])
    stage.a.state_pair(ROOT/'build', stage.PACKAGE/'previous', delivered/'pair',
                       binding['old'], binding['new'])
    return plan


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'execute'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    plan = check()
    if args.action == 'execute':
        stage.h.require(args.output is not None, 'fresh lease output required')
        stage.OUTPUT = stage.BASE/'correction'
        stage.bind()
        stage.a.check = lambda: plan  # Immutable pair, independent of later worktree edits.
        stage.a.restore(args.output.resolve())
    else:
        print('verified delivered and rollback identities; no writes or GPU work')
