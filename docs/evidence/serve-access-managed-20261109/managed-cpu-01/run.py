import unittest
from pathlib import Path
from tools.serve_recovery import test_native_ingress as legacy
legacy.INGRESS=Path('.q4t-work/serve-access-20261109/host/q4t-ingress').resolve()
legacy.NativeTests.output=Path('.q4t-work/serve-access-20261109/legacy-fixtures').resolve()
legacy.NativeTests.output.mkdir(exist_ok=True)
names=['tools.serve_recovery.test_access_management']
names += ['tools.serve_recovery.test_native_bundle.BundleTests.'+n for n in ['test_default_package_has_native_binary_and_no_nginx_dependency','test_v2_source_inventory_is_frozen_and_cannot_claim_isolated_io','test_historical_source_inventory_versions_remain_verifiable','test_launcher_never_hands_guardian_a_regular_file_for_diagnostics']]
names += ['tools.serve_recovery.test_access_io.AccessIoTests.'+n for n in ['test_real_journal_new_revision_and_large_fragmented_history','test_writer_busy_and_corrupt_policy_keep_one_worker','test_private_fds_and_missing_file_leave_native_snapshot_serving']]
names += ['tools.serve_recovery.test_native_ingress.NativeTests.test_generation_rebind_rotates_token_and_strips_forged_client_headers']
result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
raise SystemExit(not result.wasSuccessful())
