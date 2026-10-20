"""Small host-only build-identity contracts; no compiler/model execution."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('delivery_build', HERE / 'build.py')
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class Contracts(unittest.TestCase):
    output = None

    def setUp(self):
        self.work = self.output / self._testMethodName
        self.work.mkdir()

    def configuration(self, macro='-DQ4T_STARTUP_RESERVATION=1', research='OFF'):
        source, compiled = self.work / 'source', self.work / 'build'
        source.mkdir()
        compiled.mkdir()
        (compiled / 'CMakeCache.txt').write_text(
            f'Q4T_STARTUP_RESOURCE_PLAN:BOOL={research}\n'
            'Q4T_BUILD_TESTS:BOOL=ON\nCMAKE_BUILD_TYPE:STRING=Release\n'
            'Q4T_CUDA_ARCHITECTURES:STRING=110a\n'
            f'CMAKE_HOME_DIRECTORY:INTERNAL={source}\n')
        (compiled / 'compile_commands.json').write_text(json.dumps([
            dict(file=str(source / 'src/server/chat_server.cpp'),
                 command='c++ ' + macro + ' -c source.cpp')]))
        return compiled, source

    def test_product_macro_exact(self):
        compiled, source = self.configuration()
        self.assertEqual(build.verify_product_configuration(compiled, source)
                         ['research_adapter'], 'OFF')

    def test_macro_prefix_rejected(self):
        with self.assertRaises(RuntimeError):
            build.verify_product_configuration(*self.configuration(
                '-DQ4T_STARTUP_RESERVATION=10'))

    def test_research_build_rejected(self):
        with self.assertRaises(RuntimeError):
            build.verify_product_configuration(*self.configuration(research='ON'))

    def test_dependency_space_and_continuation(self):
        first, second = self.work / 'with space.h', self.work / 'next.h'
        first.touch()
        second.touch()
        dep = self.work / 'unit.o.d'
        dep.write_text('unit.o: with\\ space.h \\\n next.h\nnext.h:\n')
        self.assertEqual(build.dependency_paths(dep, self.work), {first, second})

    def test_dependency_missing_rejected(self):
        dep = self.work / 'unit.o.d'
        dep.write_text('unit.o: does-not-exist.h\n')
        with self.assertRaises(FileNotFoundError):
            build.dependency_paths(dep, self.work)

    def test_warning_forms(self):
        for message in ('warning: x', 'warning #123-D: x', 'CMake Warning at x',
                        'CMake Deprecation Warning at x'):
            self.assertIsNotNone(build.WARNING.search(message), message)
        self.assertIsNone(build.WARNING.search('100% Built target q4t'))

    def test_content_mutation_rejected(self):
        path = self.work / 'input.h'
        path.write_text('abcd')
        before = build.identity(path)
        path.write_text('efgh')
        with self.assertRaises(RuntimeError):
            build.check_identity(before)

    def test_failed_attempt_never_accepted(self):
        path = self.work / 'build.json'
        path.write_text('{}')
        (self.work / 'build-failure.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'recorded a failure'):
            build.verify(path, check_workspace=False, check_default=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    build.require(output.is_relative_to(build.BASE) and not output.exists(),
                  'new delivery evidence directory required')
    output.mkdir(parents=True)
    Contracts.output = output
    with (output / 'stdout.log').open('x') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Contracts))
    build.save(output / 'result.json', dict(status='PASS' if result.wasSuccessful()
        else 'FAILED', tests=result.testsRun, gpu_execution=False,
        compiler_execution=False, argv=sys.argv,
        sources={str(p): build.identity(p) for p in (HERE / 'build.py', Path(__file__))}))
    print(json.dumps(dict(status='PASS' if result.wasSuccessful() else 'FAILED',
                         tests=result.testsRun, output=str(output))))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
