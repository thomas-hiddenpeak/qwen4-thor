#!/usr/bin/env python3
"""Host-only rejection contracts for the driver identity recognizer."""

import struct
import unittest

from driver_identity import extract_callbacks, kernel_symbol_display, note_build_id


INIT = """
 19178: 94000000 bl 0 <shrinker_alloc>
 19178: R_AARCH64_CALL26 shrinker_alloc
 1917c: aa0003f4 mov x20, x0
 19180: b4000520 cbz x0, 19224 <nv_mem_pool_init+0x144>
 19184: 90000002 adrp x2, 0
 19184: R_AARCH64_ADR_PREL_PG_HI21 .text+0x18520
 19188: 90000001 adrp x1, 0
 19188: R_AARCH64_ADR_PREL_PG_HI21 .text+0x18a80
 1918c: 91000042 add x2, x2, #0x0
 1918c: R_AARCH64_ADD_ABS_LO12_NC .text+0x18520
 19190: 91000021 add x1, x1, #0x0
 19190: R_AARCH64_ADD_ABS_LO12_NC .text+0x18a80
 19194: 52800023 mov w3, #0x1
 19198: a9000402 stp x2, x1, [x0]
 1919c: b9001803 str w3, [x0, #24]
 191a0: f9002c13 str x19, [x0, #88]
 191a4: 94000000 bl 0 <shrinker_register>
 191a4: R_AARCH64_CALL26 shrinker_register
"""


class DriverIdentityTest(unittest.TestCase):
    def test_offsets_are_extracted_not_hardcoded(self):
        changed = INIT.replace("0x18520", "0x12500").replace(
            "0x18a80", "0x12700")
        self.assertEqual(extract_callbacks(changed)["offsets"],
                         {"count": 0x12500, "scan": 0x12700})

    def test_swapped_store_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "register/store"):
            extract_callbacks(INIT.replace("stp x2, x1", "stp x1, x2"))

    def test_inconsistent_relocation_is_rejected(self):
        changed = INIT.replace("LO12_NC .text+0x18520",
                               "LO12_NC .text+0x18524")
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            extract_callbacks(changed)

    def test_missing_relocation_is_rejected(self):
        changed = INIT.replace(
            " 1918c: R_AARCH64_ADD_ABS_LO12_NC .text+0x18520\n", "")
        with self.assertRaisesRegex(ValueError, "missing"):
            extract_callbacks(changed)

    def test_extra_clobber_is_rejected(self):
        changed = INIT.replace(" 19198:", " 19196: aa0003e2 mov x2, x0\n 19198:")
        with self.assertRaisesRegex(ValueError, "initialization length"):
            extract_callbacks(changed)

    def test_duplicate_relocation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate relocation"):
            extract_callbacks(INIT + " 191a4: R_AARCH64_CALL26 shrinker_register\n")

    def test_build_id_note_is_decoded(self):
        note = struct.pack("<III", 4, 4, 3) + b"GNU\0" + b"\x01\x02\x03\x04"
        self.assertEqual(note_build_id(note.hex()), "01020304")

    def test_truncated_build_id_is_rejected(self):
        note = struct.pack("<III", 4, 8, 3) + b"GNU\0" + b"\x01\x02\x03\x04"
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            note_build_id(note.hex())

    def test_wrong_note_type_is_rejected(self):
        note = struct.pack("<III", 4, 4, 1) + b"GNU\0" + b"\x01\x02\x03\x04"
        with self.assertRaisesRegex(ValueError, "not NT_GNU_BUILD_ID"):
            note_build_id(note.hex())

    def test_kernel_symbol_interval_is_not_elf_function_size(self):
        base = 0xffff000000000000
        nm = ("0000000000018518 0000000000000004 T nv_free_user_mapping\n"
              "0000000000018c7c 000000000000039c T nv_mem_pool_alloc_pages\n")
        loaded = (f"{base + 0x18518:x} t nv_free_user_mapping\t[nvidia]\n"
                  f"{base + 0x18520:x} t $x\t[nvidia]\n"
                  f"{base + 0x18c7c:x} t nv_mem_pool_alloc_pages\t[nvidia]\n")
        result = kernel_symbol_display(nm, loaded, base, 0x18a80)
        self.assertEqual(result["text"],
                         "nv_free_user_mapping+0x568/0x764 [nvidia]")
        self.assertEqual(result["resolved_address"], hex(base + 0x18a80))
        self.assertEqual(result["elf_symbol_size"], 4)
        self.assertTrue(result["outside_elf_symbol_body"])


if __name__ == "__main__":
    unittest.main()
