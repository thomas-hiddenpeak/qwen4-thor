"""Bounded offline checks against one already sealed candidate HTTP stage."""
import argparse
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('independent_deployment_audit_test',
                                             HERE / 'audit_deploy.py')
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


class Contracts(unittest.TestCase):
    stage = None
    raw_evidence = None

    def test_actual_sealed_sqlite_rows(self):
        self.__class__.raw_evidence = check.raw_http(check.StageView(self.stage), 2)
        self.assertEqual(len(self.raw_evidence), 2)

    def test_pickle_globals_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unexpected pickle global'):
            check.BasicUnpickler(io.BytesIO(b'cos\nsystem\n.')).load()

    def test_unrun_deployment_rejected(self):
        path = check.BASE / 'nonexistent-deployment-audit-contract'
        self.assertFalse(path.exists())
        with self.assertRaises(FileNotFoundError):
            check.run(path)

    def test_health_source_and_scope_receipt_adapter(self):
        class Health:
            path = check.BASE / 'cpu-only-health-fixture/deploy-mtp-v1'
            manifest = {'state.json': 'fixture'}
            used = set()
            def file(self, _name):
                raise AssertionError('source must resolve through the outer seal')
        stage = check.StageView(Health(), self.stage)
        self.assertEqual(stage.file('source/tools/evalscope/response_identity.py'),
                         self.stage.file('source/tools/evalscope/response_identity.py'))
        helper = check.frozen_health_helper(stage)
        self.assertEqual(helper.configuration('deploy-mtp-v1'),
                         check.deployment_configuration('deploy-mtp-v1'))
        self.assertEqual(helper.configuration('deploy-plain-v1'),
                         check.deployment_configuration('deploy-plain-v1'))
        self.assertEqual(helper.environment('deploy-mtp-v1', Health.path, {}), {})
        self.assertNotIn('--max-len', helper.server_command('deploy-mtp-v1', Health.path))

    def test_revised_budget_binds_retained_failed_attempt(self):
        build = check.read(check.BASE / 'candidate-v1/build.json')
        failure = check.audit.retained_failure(build)
        accepted = dict(retained_failures=[failure], budget=dict(
            model_starts=6, http_requests=9, preparations=7, preparation_limit=7))
        with patch.object(check.audit, 'retained_failure', return_value=failure):
            self.assertEqual(check.retained_evidence(build, accepted), failure)
            omitted = copy.deepcopy(accepted)
            omitted['retained_failures'] = []
            with self.assertRaises(ValueError):
                check.retained_evidence(build, omitted)
            changed = copy.deepcopy(accepted)
            changed['retained_failures'][0]['runtime_accepted'] = True
            with self.assertRaises(ValueError):
                check.retained_evidence(build, changed)
            changed = copy.deepcopy(accepted)
            changed['budget']['preparations'] = 6
            with self.assertRaises(ValueError):
                check.retained_evidence(build, changed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--test', action='append', default=[])
    args = parser.parse_args()
    output = args.output.resolve()
    check.require(output.is_relative_to(check.BASE) and not output.exists(),
                  'new delivery evidence directory required')
    Contracts.stage = check.audit.SealedStage(check.BASE / 'default-cold-v1')
    output.mkdir(parents=True)
    with (output / 'stdout.log').open('x') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
            unittest.TestSuite(Contracts(name) for name in args.test) if args.test else
            unittest.defaultTestLoader.loadTestsFromTestCase(Contracts))
    receipt = dict(status='PASS' if result.wasSuccessful() else 'FAILED',
        tests=result.testsRun, gpu_execution=False, cache_operations=False,
        actual_deployment_accepted=False, raw_sqlite=Contracts.raw_evidence,
        candidate_stage_seal=check.identity(Contracts.stage.manifest_path),
        sources={str(p): check.identity(p) for p in
                 (Path(__file__).resolve(), HERE / 'audit_deploy.py')})
    (output / 'result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    seal = {str(p.relative_to(output)): check.sha(p) for p in output.rglob('*') if p.is_file()}
    (output / 'sealed-files.json').write_text(json.dumps(seal, indent=2) + '\n')
    print(json.dumps(dict(status=receipt['status'], tests=result.testsRun, output=str(output))))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
