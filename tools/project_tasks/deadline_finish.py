"""Two exact offline audit corrections; no frozen source or raw event rewrite."""
import argparse
import ast
import inspect
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import deadline_retry as retry
from tools.serve_service import package as pkg


def prepare_reader():
    retry.configure()
    from tools.project_tasks import deadline_report as report
    plan = retry.check()
    name = 'tools/project_tasks/deadline_report.py'
    pkg.require(pkg.sha(ROOT/name) == plan['source_files'][name], 'frozen report changed')
    replacements = {
        "public['migrations']": "public.get('migrations', 0)",
        "json.loads(observations[0])['result']":
            "dict(json.loads(observations[0])['result'], application_budget=dict("
            "remaining_model_requests=1, remaining_tool_calls=0, final_response_required=True))",
    }
    expected = {ast.dump(ast.parse(before, mode='eval').body):
                ast.parse(after, mode='eval').body for before,after in replacements.items()}
    counts = {name:0 for name in expected}
    class Correct(ast.NodeTransformer):
        def visit_Subscript(self, node):
            key = ast.dump(node)
            if key in expected:
                counts[key] += 1
                return ast.copy_location(expected[key], node)
            return self.generic_visit(node)
    original = inspect.getsource(report.audit)
    tree = Correct().visit(ast.parse(original))
    ast.fix_missing_locations(tree)
    pkg.require(all(count == 1 for count in counts.values()), 'audit correction must match exactly twice')
    corrected = ast.unparse(tree)+'\n'
    # Execute only the known local audit function definition. Its remaining
    # assertions and module environment are identical to the frozen reader.
    exec(compile(tree, '<q4t-deadline-offline-audit>', 'exec'), report.__dict__)
    previous = report.collect
    def collect():
        data = previous()
        data['finish/archive-failure-01.json'] = (retry.ORIGINAL/'archive-failure-01.json').read_bytes()
        data['finish/deadline_finish.py'] = Path(__file__).read_bytes()
        data['finish/audit.py'] = corrected.encode()
        data['finish/corrections.json'] = json.dumps(dict(
            original_audit_sha256=report.sha(original.encode()),
            corrected_audit_sha256=report.sha(corrected.encode()), replacements=replacements,
            reason='Tool observation precedes application_budget decoration; zero migrations is omitted from public state.',
            model_starts=0, http_requests=0), indent=2).encode()+b'\n'
        secrets = [c['api_key'].encode() for c in pkg.read(plan['frontend'])['clients']]
        secrets.append(pkg.read(plan['policy'])['gateway_token'].encode())
        for name, raw in data.items():
            pkg.require(not any(key in raw for key in secrets) and not re.search(
                rb'(?m)^-----BEGIN [A-Z ]*PRIVATE KEY-----\r?$', raw), 'private record: '+name)
        return data
    report.collect = collect
    return report, corrected


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('seal', 'verify'))
    action = parser.parse_args().action
    report, corrected = prepare_reader()
    result = retry.report(action)
    manifest = pkg.read(report.DEST/'manifest.json')
    pkg.require(manifest['records']['finish/deadline_finish.py'] == pkg.sha(Path(__file__)) and
        manifest['records']['finish/audit.py'] == report.sha(corrected.encode()), 'corrected reader identity')
    print(json.dumps(result))
