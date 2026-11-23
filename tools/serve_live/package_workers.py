"""Build the optional Ubuntu 24.04 arm64 Live worker package for Jetson Thor."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_service import package

# Direct media libraries arrive through these packages' own versioned dependency
# closure. NICE, Opus and the WebRTC elements are discovered by GStreamer at
# runtime and must also be present when their transports are enabled.
DEPENDS = ('gstreamer1.0-nice', 'gstreamer1.0-plugins-base', 'gstreamer1.0-plugins-good',
           'gstreamer1.0-plugins-bad', 'libsofia-sip-ua0t64', 'libsrtp2-1',
           'libssl3t64', 'libstdc++6', 'libgcc-s1', 'libc6')


def build(workers, decision, decision_sha256, version, output):
    package.require(package.sha(decision) == decision_sha256, 'worker acceptance changed')
    value = package.read(decision)
    package.accepted_release(value, value)
    selection = package.accepted_live_workers(value, value['binary_sha256'])
    subprocess.run(['dpkg', '--validate-version', version], check=True)
    architecture = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
    package.require(architecture == 'arm64', 'this package targets Ubuntu arm64')
    output = output.absolute()
    package.require(output.suffix == '.deb' and not output.exists(), 'unused .deb output required')
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        staging = Path(temp)/'staging'
        programs = staging/'usr/lib/q4t/live-workers'
        programs.mkdir(parents=True)
        staging.chmod(0o755)
        for name, digest in selection['files'].items():
            raw = package.read_live_worker(workers/name, digest)
            package.require(raw[4:6] == b'\x02\x01' and raw[18:20] == b'\xb7\x00',
                            'Live worker must be a little-endian AArch64 ELF')
            path = programs/name
            path.write_bytes(raw)
            path.chmod(0o755)
        docs = staging/'usr/share/doc/q4t-live-workers'
        docs.mkdir(parents=True)
        (docs/'copyright').write_bytes((ROOT/'LICENSE').read_bytes())
        package.save(docs/'qualification.json', dict(selection, decision_sha256=decision_sha256))
        (docs/'qualification.json').chmod(0o644)
        control = staging/'DEBIAN'
        control.mkdir()
        (control/'control').write_text(
            'Package: q4t-live-workers\nVersion: '+version+'\nArchitecture: arm64\n'
            'Maintainer: Qwen4-Thor project\nSection: sound\nPriority: optional\n'
            'Depends: '+', '.join(DEPENDS)+'\n'
            'Description: Optional native WebRTC and SIP workers for q4t\n'
            ' Runtime media executables and their qualification for Jetson Thor.\n')
        # Explicit modes avoid propagating a private build umask to packaged
        # directories or the installed license and control file.
        for path in staging.rglob('*'):
            if path.is_dir():
                path.chmod(0o755)
        (control/'control').chmod(0o644)
        (docs/'copyright').chmod(0o644)
        built = Path(temp)/'workers.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', staging, built], check=True)
        os.link(built, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=Path, required=True)
    parser.add_argument('--decision', type=Path, required=True)
    parser.add_argument('--decision-sha256', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.workers, args.decision, args.decision_sha256, args.version, args.output)


if __name__ == '__main__':
    main()
