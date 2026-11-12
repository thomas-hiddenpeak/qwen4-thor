"""Build a relocatable management-tools snapshot, with no model activation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle
from tools.serve_service import package as engine


def check_runtime():
    engine.check_python_runtime()


def prepare(output):
    check_runtime()
    output = Path(output).absolute()
    engine.require(output.resolve() == output and any(output.is_relative_to(ROOT/p)
        for p in ('build', '.q4t-work')), 'tools output outside workspace build/work')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    python = Path(sys.executable).resolve()
    sources = {ROOT/n: 'source/'+n for n in bundle.SOURCES}
    sources.update({engine.NOTICE_ROOT/n: 'source/third_party/licenses/'+n
                    for n in engine.NOTICE_FILES})
    value = dict(version=1, profile='q4t-service-tools-v1', repository=str(ROOT),
        python=dict(path=str(python), sha256=engine.sha(python)),
        files={n: engine.sha(p) for p,n in sources.items()})
    raw = (json.dumps(value, sort_keys=True, indent=2)+'\n').encode()
    digest = hashlib.sha256(raw).hexdigest()
    target = output/digest
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix='stage-', dir=output) as temporary:
            stage = Path(temporary)/'tools';stage.mkdir(mode=0o700)
            for source, name in sources.items():
                dest = stage/name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest);dest.chmod(0o400)
            (stage/'manifest.json').write_bytes(raw)
            (stage/'manifest.json').chmod(0o400)
            engine.require(all(engine.sha(stage/n) == h for n,h in value['files'].items()),
                           'source changed while building tools')
            os.rename(stage, target)
    engine.require(engine.sha(target/'manifest.json') == digest and all(
        engine.sha(target/n) == h for n,h in value['files'].items()), 'existing tools changed')
    temporary = output/'current.new'
    if temporary.is_symlink():temporary.unlink()
    temporary.symlink_to(digest)
    os.replace(temporary, output/'current')
    (output/'snapshot.done').write_text(digest+'\n')
    return dict(snapshot=str(target), manifest_sha256=digest, model_actions=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--check-runtime', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.check_runtime:
        check_runtime();print('PASS_SERVICE_PYTHON')
    elif args.list:print(';'.join([*(str(ROOT/n) for n in bundle.SOURCES),
                                *(str(engine.NOTICE_ROOT/n) for n in engine.NOTICE_FILES)]))
    else:
        parser.error('--output is required') if args.output is None else None
        print(json.dumps(prepare(args.output)))


if __name__ == '__main__':main()
