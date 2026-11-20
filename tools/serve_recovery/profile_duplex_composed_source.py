"""Fixed source reviews for actual duplex tasks and cancellation accounting.

Original failed outputs remain in the composed light receipt and public archives.
No runtime/profile/quality claim can be extended to a different source identity.
"""

REVIEW = {'candidate_binary': 'ecd6cc23bd768a81e3fbd3fd1e59543176dc329e2ae7c8378956f09ffafe5580',
 'candidate_protocol': 'f0c82ef97660964b63fb5a245a151af98d667b43478f1c367364db7df8ac5f11',
 'previous_registry_sha256': 'd28ba7ac49169a0a4bd411ecd7e4f99cebe217b18cf7fd9e1c920e7c531f8b4f',
 'original_duplex_source_review': 'a2c7bab8cbbdbb3286ec91c161a475901cbde6e373b44d19ba416d62b97b007c',
 'audio_boundary_source_review': '9592953cdbc5f15e6e8dc90bdba8c4375dad0d57cf1bb4b5e694d6ccafaf90d7',
 'cancel_usage_source_review': 'db9fdeddcfc453ad225243bad5b003ed6203ad37184ff7d3f0562abc81f99e92',
 'cancel_usage_acceptance': '6a5214e1534ad7782972d0d0d3707e81561f2745ee80565a5b435550506eaf67',
 'model_quality_repaired': False,
 'old_failed_answers_preserved': True}

OBSERVED = {'primary': {'input_sha256': 'ba8d80c62e914bcbe49f2049cae70ead3b0739d80c0d5471a1e89f25530e8970',
             'duplex_digest': '84b74c16417c2974efedaa521fe0b3ac9207f57d2b275392feda4463116bb4f9',
             'input_digests': {'realtime': 'ec595da0f491b798d5614c0247ed24a32b3823a4e0534de92ce7c590b0a61150',
                               'semantic_vad': 'b5ce937c83b3c3fdd86816257656407424bfd0eb9bb31fea49a2660e27c407ae'}},
 'light': {'input_sha256': 'e30781260dcc7b7353bfe4bd2339d2a2ff9193c9798f62ba1b2e82c25581d16c',
           'duplex_digest': 'a6ae3978c98d711f7e89ab56a03cf4bb9ffc55fc626b2b90d9b7699dbc9e9318',
           'input_digests': {'realtime': '7bbe23127c2f38d73daf173b0593aaca740270e3d3b43b65cbe4ad42b4ed25b5',
                             'semantic_vad': 'aa1ba2307435330595a7456fa3bd5469975df7f7d1954e9fa132d1959290c76c'}}}
