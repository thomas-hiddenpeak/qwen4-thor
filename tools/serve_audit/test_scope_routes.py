import unittest

from tools.serve_audit.scope_routes import compare


class ScopeRouteTest(unittest.TestCase):
    def test_declared_methods_preserve_scope_and_query_contract(self):
        def required(path):
            return dict(path=path, tasks=['A08'],
                        scope='required_inference_or_application_service')
        scope = dict(endpoints=[
            required('/objects/{object_id}'), required('/objects/{object_id}?beta=true'),
            required('/missing'), dict(path='/billing', scope='excluded_platform_management_by_user')])
        query = dict(name='beta', schema=dict(enum=['true']), **{'in': 'query'})
        item = dict(parameters=[{'$ref': '#/components/parameters/selector'}],
                    get={}, post=dict(parameters=[dict(query, schema=dict(const='false'))]))
        doc = dict(paths={'/v1/objects/{id}': item, '/v1/billing': dict(get={})},
                   components=dict(parameters=dict(selector=query)))
        rows = compare(scope, doc)
        self.assertEqual([row['issued_methods'] for row in rows],
                         [['GET', 'POST'], ['GET'], []])
        self.assertIsNone(rows[-1]['issued_path'])
        query['schema'] = dict(type='string')
        self.assertEqual(compare(scope, doc)[1]['issued_methods'], [])

    def test_ambiguous_templates_are_rejected(self):
        doc = dict(paths={'/v1/objects/{id}': {}, '/v1/objects/{name}': {}})
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            compare(dict(endpoints=[]), doc)
