"""Backport only explicit cold-probe awareness to a retained controller.

This produces a separately sealed payload. It never edits the accepted origin,
sets a false continuous-model hint, replaces its controller wholesale, or grants
authority. Both cold and continuous paths still ask the real Guardian and run
the original bounded probe. The exact two source edits and optional existing
profile-state shim are independently reconstructed during verification.
"""
import ast
import copy
from pathlib import Path
import shutil

from tools.serve_recovery import bundle, ledger, profile_compat, profile_policy
from tools.serve_service import package as engine

SOURCE = 'source/tools/serve_recovery/controller.py'
INITIAL = "        self.continuous = os.environ['Q4T_CONTINUOUS_MODEL_RECOVERY'] == '1'\n"
COLD = "        self.cold_device_recovery = os.environ.get('Q4T_COLD_DEVICE_RECOVERY') == '1'\n"
OLD_GUARD = '            if not self.continuous:\n'
NEW_GUARD = '            if not (self.continuous or self.cold_device_recovery):\n'


def adapted(raw):
    ledger.require(type(raw) is bytes and len(raw) <= 1024**2, 'cold adapter source bound')
    text = raw.decode('utf-8')
    tree = ast.parse(text)
    # Existing uses of continuous must have the reviewed semantics: one input
    # hint, one post-model assignment, and exactly one preflight eligibility
    # load. No other behavior may be affected by adding a second eligibility.
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Controller'), None)
    ledger.require(cls is not None, 'cold adapter needs the retained controller')
    uses = [node for node in ast.walk(cls) if isinstance(node, ast.Attribute) and
            isinstance(node.value, ast.Name) and node.value.id == 'self' and node.attr == 'continuous']
    ledger.require(len(uses) == 3 and sum(isinstance(n.ctx, ast.Load) for n in uses) == 1 and
        text.count(INITIAL) == 1 and text.count(OLD_GUARD) == 1 and COLD not in text and
        not any(isinstance(n, ast.Attribute) and n.attr == 'cold_device_recovery' for n in ast.walk(cls)),
        'retained controller requires a reviewed cold adapter')
    preflight = next((n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'preflight'), None)
    ledger.require(preflight is not None and any(n in uses and isinstance(n.ctx, ast.Load)
        for n in ast.walk(preflight)), 'legacy hint has another meaning')
    return text.replace(INITIAL, INITIAL+COLD).replace(OLD_GUARD, NEW_GUARD).encode()


def aware(package):
    raw = (Path(package)/SOURCE).read_bytes()
    # Verify reversibility of the exact backport, rather than a substring
    # capability claim. Other controller methods remain those of this payload.
    text = raw.decode('utf-8')
    if text.count(COLD) != 1 or text.count(NEW_GUARD) != 1:
        return False
    old = text.replace(COLD, '').replace(NEW_GUARD, OLD_GUARD).encode()
    try: return adapted(old) == raw
    except ValueError: return False


def verify(original, original_sha, target, target_sha):
    original, target = Path(original).resolve(strict=True), Path(target).resolve(strict=True)
    first = bundle.verify(original, original_sha); second = bundle.verify(target, target_sha)
    ledger.require(12 <= first['version'] <= 16, 'cold adapter needs a retained legacy service')
    expected = copy.deepcopy(first)
    expected_bytes = adapted((original/SOURCE).read_bytes())
    import hashlib
    expected['files'][SOURCE] = hashlib.sha256(expected_bytes).hexdigest()
    # Compose only with the already defined profile-history representation shim.
    profile = profile_compat.SOURCE
    if first['files'][profile] != second['files'].get(profile):
        expected['files'][profile] = engine.sha(Path(profile_policy.__file__))
    ledger.require(second == expected and (target/SOURCE).read_bytes() == expected_bytes,
                   'cold adapter changed another payload contract')
    return dict(format='q4t-cold-controller-compatibility-v1', original=str(original),
        original_manifest_sha256=original_sha, payload=str(target), payload_manifest_sha256=target_sha,
        controller_sha256=expected['files'][SOURCE], engine_manifest_sha256=first['engine_manifest_sha256'],
        model_actions=False, business_acceptance=False)


def prepare(original, original_sha, output, *, profile_history=True):
    original = Path(original).resolve(strict=True)
    manifest = bundle.verify(original, original_sha)
    ledger.require(12 <= manifest['version'] <= 16 and manifest.get('recovery_profiles') is not None,
                   'cold adapter needs a qualified retained native payload')
    raw = adapted((original/SOURCE).read_bytes())
    ledger.require(not Path(output).resolve().is_relative_to(original), 'cold adapter output overlaps origin')
    target = engine.private_directory(output, Path(manifest['repository']))
    shutil.copytree(original, target, dirs_exist_ok=True, symlinks=True)
    replacements = {SOURCE: raw}
    if profile_history:
        replacements[profile_compat.SOURCE] = Path(profile_policy.__file__).read_bytes()
    for name, data in replacements.items():
        source = target/name; engine.regular(source)
        source.chmod(0o600); source.write_bytes(data); source.chmod(0o400)
        manifest['files'][name] = engine.sha(source)
    engine.save(target/'manifest.json', manifest)
    digest = engine.sha(target/'manifest.json')
    launcher = target/'bin/q4t'; engine.regular(launcher)
    launcher.chmod(0o700); launcher.write_bytes(bundle.launcher(digest, manifest['python']['path']))
    launcher.chmod(0o500)
    return verify(original, original_sha, target, digest)
