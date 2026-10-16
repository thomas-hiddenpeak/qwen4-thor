"""Opt-in diagnostic hooks for the pinned EvalScope request implementation.

Does not edit the installed package. Caller supplies synchronous start/first
callbacks and owns counter cleanup. Never use this as performance acceptance.
"""
import ast
import hashlib
import inspect
from pathlib import Path
import textwrap

EXPECTED_MODULE_SHA256 = '49bb0179f4fc06657a4ac06513d9aab8181a23411fa0caca13d1444cc58ce83b'


def instrument(function, start, first, expected_sha256=EXPECTED_MODULE_SHA256):
    """Return a patched function only if both identity and AST anchors match.

    start() executes after JSON serialization, immediately before the existing
    request clock. first(st, timestamp) executes after the original streaming
    TTFT assignment; timestamp remains EvalScope's original pre-JSON timestamp.
    """
    source_file = Path(inspect.getsourcefile(function))
    actual = hashlib.sha256(source_file.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f'EvalScope implementation identity mismatch: {actual}')
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))

    class Hooks(ast.NodeTransformer):
        starts = 0
        firsts = 0

        def visit_Assign(self, node):
            if (len(node.targets) == 1 and
                    ast.unparse(node.targets[0]) == 'st' and
                    ast.unparse(node.value) == 'time.perf_counter()'):
                self.starts += 1
                return [ast.parse('_ucf_start()').body[0], node]
            if (len(node.targets) == 1 and
                    ast.unparse(node.targets[0]) == 'output.first_chunk_latency' and
                    ast.unparse(node.value) == 'timestamp - st'):
                self.firsts += 1
                return [node, ast.parse('_ucf_first(st, timestamp)').body[0]]
            return node

    hooks = Hooks()
    tree = ast.fix_missing_locations(hooks.visit(tree))
    if (hooks.starts, hooks.firsts) != (1, 1):
        raise ValueError(f'Boundary anchors changed: {hooks.starts}/{hooks.firsts}')
    namespace = dict(function.__globals__, _ucf_start=start, _ucf_first=first)
    exec(compile(tree, str(source_file) + ':ucf-diagnostic', 'exec'), namespace)
    return namespace[function.__name__]
