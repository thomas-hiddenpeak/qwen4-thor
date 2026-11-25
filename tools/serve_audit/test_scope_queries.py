import unittest

from tools.serve_audit.scope_queries import compare, compare_defaults


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

    def test_explicit_defaults_refs_overrides_and_types(self):
        def field(name, schema):
            return dict(name=name, schema=schema, **{'in': 'query'})

        def spec(path, fields, **components):
            return dict(components=components, paths={path: dict(get=dict(
                responses={'200': {}}, parameters=fields))})

        scope = dict(endpoints=[dict(path='/things', tasks=['A01'],
                     scope='required_inference_or_application_service')])
        upstream = spec('/things', [
            field('include', dict(type='array', default=[])),
            field('limit', dict(allOf=[{'$ref': '#/components/schemas/in~1t'}])),
            field('quiet', dict(type='boolean', default=False)),
            field('cursor', dict(default=None)),
            field('order', dict(default='desc')),
            field('optional', dict(anyOf=[dict(default='conditional'), {}])),
        ], schemas={'in/t': dict(default=20)})
        issued = spec('/v1/things', [
            field('include[]', dict(default=[])),
            field('limit', {'$ref': '#/components/schemas/alias'}),
            field('quiet', dict(default=0)),
            field('order', dict(default='asc', allOf=[dict(default='desc')])),
        ], schemas={'alias': {'$ref': '#/components/schemas/integer', 'default': 20.0},
                    'integer': {'type': 'integer'}})
        report = compare_defaults(scope, upstream, issued)
        self.assertEqual(report['query_parameters'], 6)
        self.assertEqual(report['explicit_defaults'], 5)
        self.assertEqual({r['query']: r['state'] for r in report['rows']}, {
            'include': 'matching', 'limit': 'matching', 'quiet': 'different',
            'cursor': 'missing', 'order': 'ambiguous'})
        self.assertEqual(next(r for r in report['rows'] if r['query'] == 'cursor')['official'], [None])
