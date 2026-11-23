import unittest

from .scope_methods import compare


class ScopedMethodsTest(unittest.TestCase):
    def test_selector_missing_status_and_excluded_platform(self):
        paths = ['/jobs', '/jobs?beta=true', '/platform']
        scope = dict(endpoints=[dict(path=p, tasks=['A08'],
            scope=('excluded_platform_management_by_user' if p == '/platform'
                   else 'required_inference_or_application_service')) for p in paths])
        def operation(code):
            return dict(responses={code: {}, 'default': {}})
        official = dict(paths={'/jobs': dict(get=operation('200'), post=operation('201')),
                               '/jobs?beta=true': dict(post=operation('201')),
                               '/platform': dict(delete=operation('200'))})
        issued = dict(paths={'/v1/jobs': dict(get=operation('200'), post=operation('200'))})
        result = compare(scope, official, issued)
        self.assertEqual((result['required_methods'], result['missing_declaration'],
                          result['exact_success_declarations'],
                          result['declared_with_different_success']), (3, 1, 1, 1))
        del official['paths']['/jobs?beta=true']
        with self.assertRaisesRegex(ValueError, 'catalogue'):
            compare(scope, official, issued)
