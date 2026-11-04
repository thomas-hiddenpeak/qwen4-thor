"""Real temporary files: recovery, foreign state and interrupted replacement."""
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.serve_lifecycle import catalog, deployment as d

h = d.h
CPU = d.ROOT/'.q4t-work/serve-lifecycle-20261103/cpu'


class Files(unittest.TestCase):
    def setUp(self):
        CPU.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CPU)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('build', '.q4t-work', 'deploy/serve-ops'):
            (self.root/name).mkdir(parents=True)
        self.previous = self.root/'.q4t-work/old'
        self.previous.mkdir()
        self.write_pair(self.previous, b'previous ELF', version=3)
        self.write_pair(self.root/'build', b'current ELF', version=4,
                        previous_binary=str(self.previous/'q4t'),
                        previous_release=str(self.previous/'q4t.release.json'))
        for name, raw in (('q4t.rules.yml', 'current rules'), ('q4t-v3.rules.yml', 'legacy rules')):
            (self.root/'deploy/serve-ops'/name).write_text(raw)
        self.bundle = self.root/'.q4t-work/bundle'
        self.manifest = d.freeze(self.root, self.bundle)
        self.number = 0

    def write_pair(self, folder, raw, *, version, **extra):
        (folder/'q4t').write_bytes(raw)
        (folder/'CMakeCache.txt').write_text('cache '+raw.decode())
        self.write_json(folder/'q4t.release.json', dict(
            binary_sha256=h.sha(folder/'q4t'), build_cache=str(folder/'CMakeCache.txt'),
            build_cache_sha256=h.sha(folder/'CMakeCache.txt'),
            chat_protocol=dict(protocol_sha256=str(version)*64,
                               observability=dict(version=version)), **extra))

    @staticmethod
    def write_json(path, value):path.write_text(json.dumps(value))

    def output(self):
        self.number += 1
        result = self.root/'.q4t-work'/f'transaction-{self.number}'
        result.mkdir()
        return result

    def marker(self):
        path = d.marker_path(self.root)
        d.publish_marker(path, dict(version=1, bundle=str(self.bundle),
            manifest_sha256=h.sha(self.bundle/'manifest.json'),
            destination=str(self.root/'build'), target='previous',
            initial=d.state(self.root, self.manifest), journal=str(self.output()), started=1))
        return path

    def installed(self):return {n:(self.root/'build'/n).read_bytes() for n in d.NAMES}

    def test_switch_roundtrip_preserves_pair_and_selects_rules(self):
        original = self.installed()
        for role, version in (('previous', 3), ('current', 4)):
            output = self.output()
            result = d._apply(self.root, self.bundle, role, output)
            self.assertEqual(result['role'], role)
            pair = catalog.pair(self.root/'build/q4t', self.root/'build/q4t.release.json')
            self.assertEqual(pair['observability_version'], version)
            self.assertEqual(h.read(output/'after.json')['selected_rules'],
                             self.manifest['releases'][role]['rules'])
            self.assertFalse(d.marker_path(self.root).exists())
        self.assertEqual(self.installed(), original)

    def test_all_four_known_partial_states_recover_only_current(self):
        for roles in itertools.product(d.ROLES, repeat=2):
            with self.subTest(roles=roles):
                # Initial receipt is a complete known pair, interruption may
                # leave either file old or new. It is never a source of hashes.
                self.marker()
                for role, name in zip(roles, d.NAMES):
                    (self.root/'build'/name).write_bytes((self.bundle/role/name).read_bytes())
                value = d._apply(self.root, self.bundle, 'current', self.output(), recovering=True)
                self.assertEqual(value['role'], 'current')
                self.assertFalse(d.marker_path(self.root).exists())

    def test_second_replace_failure_preserves_marker_then_recovers(self):
        replace = h.replace_checked
        def fail_second(source, destination, digest):
            if Path(destination).name == 'q4t.release.json':raise OSError('injected disk error')
            return replace(source, destination, digest)
        with patch.object(h, 'replace_checked', side_effect=fail_second), self.assertRaises(OSError):
            d._apply(self.root, self.bundle, 'previous', self.output())
        self.assertIsNone(d.state(self.root, self.manifest)['role'])
        self.assertTrue(d.marker_path(self.root).exists())
        self.assertEqual(d._apply(self.root, self.bundle, 'current', self.output(),
                                 recovering=True)['role'], 'current')

    def test_unmarked_mixed_or_unknown_installed_files_are_never_overwritten(self):
        target = self.root/'build/q4t'
        for raw in ((self.bundle/'previous/q4t').read_bytes(), b'foreign ELF'):
            target.write_bytes(raw)
            before = self.installed()
            with self.assertRaises(RuntimeError):d._apply(self.root, self.bundle, 'current', self.output())
            self.assertEqual(self.installed(), before)
            self.assertFalse(d.marker_path(self.root).exists())

    def test_foreign_and_malformed_recovery_markers_fail_closed(self):
        marker = self.marker();original = h.read(marker)
        for field, value in (('bundle', '/foreign'), ('version', True),
                             ('manifest_sha256', '0'*64), ('target', 'other'),
                             ('initial', {}), ('started', float('inf')), ('journal', 'relative')):
            with self.subTest(field=field):
                self.write_json(marker, dict(original, **{field:value}))
                before = self.installed()
                with self.assertRaises((RuntimeError, ValueError)):
                    d._apply(self.root, self.bundle, 'current', self.output(), recovering=True)
                self.assertEqual(self.installed(), before)
                self.assertTrue(marker.exists())

    def test_other_deployment_marker_and_symlink_are_preserved(self):
        foreign = self.root/'build/another-recovery.json';foreign.write_text('{}')
        before = self.installed()
        with self.assertRaises(RuntimeError):d._apply(self.root, self.bundle, 'previous', self.output())
        self.assertEqual(self.installed(), before)
        foreign.unlink()
        d.marker_path(self.root).symlink_to(self.previous/'q4t.release.json')
        with self.assertRaises(OSError):
            d._apply(self.root, self.bundle, 'current', self.output(), recovering=True)
        self.assertEqual(self.installed(), before)

    def test_source_tamper_prevents_even_first_mutation(self):
        (self.bundle/'previous/q4t').write_bytes(b'tampered')
        before = self.installed()
        with self.assertRaises(RuntimeError):d._apply(self.root, self.bundle, 'previous', self.output())
        self.assertEqual(self.installed(), before)
        self.assertFalse(d.marker_path(self.root).exists())

    def test_marker_changed_after_first_replace_prevents_second_replace(self):
        replace = h.replace_checked
        before = self.installed()
        def change_marker(source, destination, digest):
            replace(source, destination, digest)
            marker = d.marker_path(self.root)
            value = h.read(marker);value['target'] = 'current'
            self.write_json(marker, value)
        with patch.object(h, 'replace_checked', side_effect=change_marker), self.assertRaises(RuntimeError):
            d._apply(self.root, self.bundle, 'previous', self.output())
        self.assertEqual((self.root/'build/q4t.release.json').read_bytes(), before['q4t.release.json'])
        self.assertTrue(d.marker_path(self.root).exists())

    def test_durable_marker_is_exclusive_and_lock_contention_fails(self):
        marker = self.marker();original = marker.read_bytes()
        with self.assertRaises(FileExistsError):d.publish_marker(marker, {'replacement':True})
        self.assertEqual(marker.read_bytes(), original)
        self.assertFalse(list(marker.parent.glob('.release-marker-*')))
        with d.deployment_lock(self.root), self.assertRaises(BlockingIOError):
            with d.deployment_lock(self.root):pass

    def test_output_cannot_escape_or_overwrite_and_destination_cannot_link(self):
        for path in (self.root/'result', self.bundle):
            with self.assertRaises(RuntimeError):d.writable_output(self.root, path)
        build = self.root/'build';build.rename(self.root/'moved-build')
        build.symlink_to(self.root/'moved-build', target_is_directory=True)
        with self.assertRaises(RuntimeError):d.state(self.root, self.manifest)

    def test_missing_gpu_guard_or_busy_device_blocks_transaction(self):
        fresh = self.root/'.q4t-work/public-attempt'
        with patch.object(d, 'ROOT', self.root), patch.object(h, 'require_guard',
                side_effect=RuntimeError('no lease')), self.assertRaises(RuntimeError):
            d.transact(self.root, self.bundle, 'previous', fresh, self.root)
        self.assertFalse(fresh.exists())
        before = self.installed()
        with patch.object(d, 'ROOT', self.root), patch.object(h, 'require_guard'), \
             patch.object(d.shared, 'require_idle_gpu', side_effect=d.shared.EnvironmentAbort('occupied')), \
             self.assertRaises(d.shared.EnvironmentAbort):
            d.transact(self.root, self.bundle, 'previous', fresh, self.root)
        self.assertEqual(self.installed(), before)
        self.assertEqual(h.read(fresh/'result.json')['status'], 'ENV_ABORT')
        self.assertFalse(d.marker_path(self.root).exists())

    def test_catalog_root_and_explicit_stale_boundary_are_distinct(self):
        result = catalog.history(self.root/'build/q4t', self.root/'build/q4t.release.json')
        self.assertEqual(result['state'], 'VERIFIED_TO_ROOT')
        self.assertEqual(len(result['pairs']), 2)
        raw = h.read(self.root/'build/q4t.release.json')
        raw['previous_release_was_stale'] = True
        self.write_json(self.root/'build/q4t.release.json', raw)
        (self.previous/'q4t.release.json').write_text('deliberately stale archival bytes')
        result = catalog.history(self.root/'build/q4t', self.root/'build/q4t.release.json')
        self.assertEqual(result['state'], 'VERIFIED_TO_LEGACY_BOUNDARY')
        self.assertEqual(len(result['pairs']), 1)
        self.assertFalse(result['terminal']['installable'])
        with self.assertRaises(RuntimeError):d.freeze(self.root, self.root/'.q4t-work/no-legacy-install')

    def test_catalog_rejects_cycles_mismatched_hashes_and_invalid_flags(self):
        path = self.root/'build/q4t.release.json';original = h.read(path)
        variants = [dict(original, binary_sha256='0'*64), dict(original, build_cache_sha256='0'*64),
                    dict(original, previous_release_was_stale=1),
                    dict(original, previous_binary=str(self.root/'build/q4t'), previous_release=str(path))]
        for value in variants:
            self.write_json(path, value)
            with self.assertRaises(RuntimeError):catalog.history(self.root/'build/q4t', path)

    def test_catalog_missing_cache_symlink_and_partial_history_are_not_verified(self):
        path = self.root/'build/q4t.release.json';original = h.read(path)
        partial = dict(original);del partial['previous_release']
        self.write_json(path, partial)
        with self.assertRaises(RuntimeError):catalog.history(self.root/'build/q4t', path)
        self.write_json(path, original)
        cache = self.root/'build/CMakeCache.txt';cache.unlink()
        with self.assertRaises(RuntimeError):catalog.pair(self.root/'build/q4t', path)
        cache.symlink_to(self.previous/'CMakeCache.txt')
        with self.assertRaises(RuntimeError):catalog.pair(self.root/'build/q4t', path)


if __name__ == '__main__':unittest.main()
