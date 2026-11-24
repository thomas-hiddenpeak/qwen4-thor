import unittest

from tools.serve_audit.scope_queries import compare


class QueryTests(unittest.TestCase):
    def test_refs_overrides_array_spelling_and_missing_method(self):
        scope = dict(endpoints=[dict(path='/things', tasks=['A01'],
                    scope='required_inference_or_application_service')])
        def param(name, kind):
            return dict(name=name, schema=dict(type=kind), **{'in':'query'})
        op = dict(responses={'200':{}})
        official = dict(components=dict(parameters={'x/y':param('include','string')}),
            paths={'/things':dict(parameters=[{'$ref':'#/components/parameters/x~1y'}],
                get=dict(op, parameters=[param('include','array'), param('padding','boolean'),
                                         param('cursor','string')]), post=op)})
        issued = dict(paths={'/v1/things':dict(get=dict(op, parameters=[
            param('include[]','array'),param('cursor[]','array')]))})
        report = compare(scope, official, issued)
        self.assertEqual(report['declared_methods'], 1)
        self.assertEqual(report['required_methods'], 2)
        self.assertEqual({r['query']:r['state'] for r in report['rows']},
                         dict(include='bracket_name_only',padding='missing',cursor='missing'))
        with self.assertRaisesRegex(ValueError, 'Duplicate query parameter'):
            issued['paths']['/v1/things']['get']['parameters'].append(param('include[]','array'))
            compare(scope, official, issued)
