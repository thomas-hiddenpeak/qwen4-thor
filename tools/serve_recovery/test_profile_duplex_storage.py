import unittest
from tools.serve_recovery import profile_duplex_reuse as storage


class SharedTranscript(unittest.TestCase):
    def setUp(self):
        self.text = '音\n' * 8192
        self.row = {'events':[{'audio':self.text}, {'audio':self.text}], 'unchanged':[0,None]}

    def test_exact_roundtrip_and_deterministic_dictionary(self):
        value = storage.pack_text(self.row)
        self.assertEqual(value['strings'], [self.text])
        self.assertEqual(storage.unpack_text(value), self.row)
        other = dict(reversed(list(self.row.items())))
        self.assertEqual(storage.pack_text(other), value)
        small = {'a':['once', 'other']}
        self.assertEqual(storage.unpack_text(storage.pack_text(small)), small)

    def test_invalid_indices_expansion_and_identity_are_rejected(self):
        for bad in [True, -1, 1, '0']:
            changed = storage.pack_text(self.row)
            changed['witness']['events'][0]['audio'][storage.TEXT_REF] = bad
            with self.assertRaises(ValueError):
                storage.unpack_text(changed)
        safe = storage.pack_text(self.row)
        for field, value in [('bytes',0), ('bytes',storage.TEXT_BYTES+1), ('sha256','0'*64),
                             ('strings',[self.text,self.text]), ('witness',[{storage.TEXT_REF:0}]*256)]:
            with self.assertRaises(ValueError):
                storage.unpack_text(dict(safe, **{field:value}))
        with self.assertRaises(ValueError):
            storage.pack_text({storage.TEXT_REF:3})
