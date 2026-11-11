"""Seal a narrowly adapted old payload for current durable profile history.

The old engine, controller, entrypoint and assets remain byte-identical. Only
the shared profile-policy representation is replaced by the trusted recovery
root's reader/writer. The delta and both original manifests are independently
verifiable; this is not an acceptance claim for a new engine or controller.
"""
import copy
from pathlib import Path
import shutil

from tools.serve_recovery import bundle, ledger, profile_policy
from tools.serve_service import package as engine

SOURCE = 'source/tools/serve_recovery/profile_policy.py'
FORMAT = 'q4t-profile-compatibility-v1'


def retained_origin(history, target, target_sha):
    """An adapted rollback must derive from an actual retained descriptor."""
    ledger.require(type(history) is list and len(history) <= 16,
                   'profile adapter history bound')
    for entry in reversed(history):
        try:
            return verify(entry['package'], entry['manifest_sha256'], target, target_sha)
        except (ValueError, RuntimeError, OSError):
            continue
    return None


def verify(original, original_sha, target, target_sha):
    original = Path(original).resolve(strict=True)
    target = Path(target).resolve(strict=True)
    first = bundle.verify(original, original_sha)
    second = bundle.verify(target, target_sha)
    ledger.require(first['version'] >= 12 and first.get('recovery_profiles') is not None,
                   'profile adapter requires a qualified original payload')
    adapter_sha = engine.sha(Path(profile_policy.__file__))
    expected = copy.deepcopy(first)
    expected['files'][SOURCE] = adapter_sha
    ledger.require(second == expected, 'profile adapter changed another payload contract')
    # The outer manifest pins the same engine manifest; verify() above checks
    # both actual engine directories, every original file and all model assets.
    return dict(format=FORMAT, original=str(original), original_manifest_sha256=original_sha,
        payload=str(target), payload_manifest_sha256=target_sha,
        adapter_source=SOURCE, adapter_sha256=adapter_sha,
        engine_manifest_sha256=first['engine_manifest_sha256'],
        qualified_registry_sha256=ledger.digest(first['recovery_profiles']),
        model_actions=False, business_acceptance=False)


def prepare(original, original_sha, output):
    original = Path(original).resolve(strict=True)
    manifest = bundle.verify(original, original_sha)
    ledger.require(manifest['version'] >= 12 and
                   manifest.get('recovery_profiles') is not None,
                   'profile adapter requires a qualified original payload')
    ledger.require(not Path(output).resolve().is_relative_to(original),
                   'adapter output is inside original payload')
    target = engine.private_directory(output, Path(manifest['repository']))
    shutil.copytree(original, target, dirs_exist_ok=True, symlinks=True)
    source = target/SOURCE
    engine.regular(source)
    source.chmod(0o600)
    source.write_bytes(Path(profile_policy.__file__).read_bytes())
    source.chmod(0o400)
    manifest['files'][SOURCE] = engine.sha(source)
    engine.save(target/'manifest.json', manifest)
    digest = engine.sha(target/'manifest.json')
    launcher = target/'bin/q4t'
    engine.regular(launcher)
    launcher.chmod(0o700)
    launcher.write_bytes(bundle.launcher(digest, manifest['python']['path']))
    launcher.chmod(0o500)
    return verify(original, original_sha, target, digest)
