from pathlib import Path
base=Path('.q4t-work/serve-cache-io-20261109')
s=Path('.q4t-work/serve-chat-storage-20261109/prepare_delivery.py').read_text()
s=s.replace('Seal accepted application APIs without repeating unchanged business tests.', 'Seal accepted native progress and optional cache isolation with reused unchanged evidence.')
s=s.replace("BASE = ROOT / '.q4t-work/serve-chat-storage-20261109'", "BASE = ROOT / '.q4t-work/serve-cache-io-20261109'")
s=s.replace("BASE / 'business-03/plan.json'", "BASE / 'business-01/plan.json'")
s=s.replace("EVIDENCE / 'serve-chat-business-20261109/decision.json'", "EVIDENCE / 'serve-cache-io-business-20261109/decision.json'")
s=s.replace('ACCEPT_TARGETED_STORED_CHAT','ACCEPT_TARGETED_CACHE_IO_AND_NATIVE_PROGRESS')
s=s.replace('actual stored Chat acceptance','actual cache isolation acceptance')
a=s.index('    groups = (');b=s.index('    indices = {}',a)
s=s[:a]+'''    groups = ('serve-cache-io-cpu-20261109', 'serve-cache-io-driver-20261109',
        'serve-cache-io-business-20261109', 'serve-progress-core-20261109',
        'serve-progress-native-20261109', 'serve-progress-controller-20261109',
        'serve-progress-stages-20261109', 'serve-progress-resource-20261109',
        'serve-progress-business-20261109', 'serve-chat-delivery-20261109')
'''+s[b:]
a=s.index('    passed = [');b=s.index("    protocol = caps[",a)
s=s[:a]+'''    passed = [
        'serve-cache-io-cpu-20261109/'+n for n in ('primitive-01', 'integration-01',
        'session-02', 'prefix-02', 'incremental-01', 'target-01', 'target-02',
        'health-01', 'inventory-02', 'declarations-02', 'build-05')]
    passed += ['serve-progress-controller-20261109/'+n for n in
               ('controller-cpu-01', 'rollback-verify-01')]
    passed += ['serve-progress-resource-20261109/'+n for n in
               ('resource-cpu-01', 'resource-python-01')]
    require(all(read(EVIDENCE / n / 'result.json')['exit_code'] == 0 for n in passed),
            'targeted prerequisite failed')
    caps = read(BASE / 'business-01/capabilities.json')
    config = read(BASE / 'business-01/configuration.json')
'''+s[b:]
a=s.index('    changed = {');b=s.index("    require(sha(BUILD / 'q4t-ingress')",a)
s=s[:a]+'''    changed = {n: dict(before=sha(old / 'source' / n) if (old / 'source' / n).exists() else None, after=h)
               for n, h in control.items() if not (old / 'source' / n).exists() or sha(old / 'source' / n) != h}
    require(set(changed) == {'tools/serve_recovery/'+n+'.py' for n in
        ('bundle', 'child', 'controller', 'policy', 'progress')}, 'unexpected control change')
    recovery = read(EVIDENCE / 'serve-progress-business-20261109/business-02/plan.json')
    require(all(recovery['sources'][n] == h for n,h in control.items()
                if n != 'tools/serve_recovery/progress.py'), 'unreviewed recovery control drift')
    # The only later control change adds cache/resource work=4 to the same
    # detector. Targeted resource-python CPU and actual cache wait are accepted.
    require(control['tools/serve_recovery/progress.py'] ==
        'f008b5f27a88cccff92232c3cc385722f67869445f9c7d8c216c1ab761'.replace(
        'f49c7', 'f49c7'), 'unused') if False else None
'''+s[b:]
# Replace the placeholder with the exact audited post-resource identity.
s=s.replace("    require(control['tools/serve_recovery/progress.py'] ==\n        'f008b5f27a88cccff92232c3cc385722f67869445f9c7d8c216c1ab761'.replace(\n        'f49c7', 'f49c7'), 'unused') if False else None\n", "    require(control['tools/serve_recovery/progress.py'] ==\n        'f008b5f27a88cccff92232c3cc385722f67869445f9d75f49c7d8c216c1ab761',\n        'cache progress budget source drift')\n")
s=s.replace('PASS_TARGETED_STORED_CHAT_DELIVERY','PASS_TARGETED_CACHE_PROGRESS_DELIVERY')
s=s.replace("previous_application_acceptance_reused=str(EVIDENCE / 'serve-application-delivery-20261109/README.md')", "previous_application_acceptance_reused=str(EVIDENCE / 'serve-chat-delivery-20261109/README.md')")
a=s.index("        coverage='Native stored Chat routes");b=s.index("    metadata = read",a)
s=s[:a]+'''        coverage='Native controller v5 actual same-controller model recovery and observation loss are accepted. '
        'Cache resource work budget has direct CPU coverage and actual file waits do not cause false recovery. '
        'Ingress/Guardian/CUDA probe bytes are unchanged and previous accepted behavior is reused.',
        reused_cpu={n: sha(EVIDENCE / n / 'result.json') for n in passed},
        recovery_business=str(EVIDENCE / 'serve-progress-business-20261109/decision.json'),
        actual_configuration_provenance='declarations/configuration.json is unmodified actual cache validation '
        '(16384 max length, cache enabled). Bundle settings remain managed 8192/cache off; '
        'that profile used actual R03 validation. No projected config is labeled observed.',
        additional_gpu_tests=0))
'''+s[b:]
s=s.replace("version='serve-stored-chat-20261109'", "version='serve-native-progress-cache-20261109'")
a=s.index("        source_commit=commit, source_note=");b=s.index("        build_cache=",a)
s=s[:a]+'''        source_commit=commit, source_note='Native actual-completion progress and evidence-based recovery; '
        'isolated optional cache filesystem with bounded recovery, same-model business continuity; '
        'previous application protocols retained.',
'''+s[b:]
s=s.replace("'Exact runtime discovery and stored Chat JSON/SSE/tools/fault evidence; '\n        'unaffected prior Responses/Conversations and other protocols reuse accepted evidence.'", "'Exact cache-validation runtime discovery; R03 actual recovery and cache JSON/SSE/fault evidence. '\n        'Unchanged stored Chat/Responses/Conversations and other protocols reuse accepted evidence.'")
s=s.replace("notices = metadata['third_party_notices']", "metadata['resilience_acceptance'] = dict(native_progress=True, evidence_based_model_recovery=True,\n        observation_loss_same_model=True, optional_cache_io_isolated=True, cache_recovery_bounded=True,\n        cache_timeout_does_not_restart_model=True, delivery_configuration_is_observed_config=False)\n    notices = metadata['third_party_notices']")
s=s.replace("build=str(EVIDENCE / 'serve-chat-storage-20261109/build-04/result.json')", "build=str(EVIDENCE / 'serve-cache-io-cpu-20261109/build-03/result.json')")
s=s.replace("native_cpu=[str(EVIDENCE / n / 'result.json') for n in passed if\n                        n.endswith('/native-01') or n.endswith('/tls-01')]", "native_cpu_reused_from=str(old / 'native-build.json')")
(base/'prepare_delivery.py').write_text(s)
s=Path('.q4t-work/serve-chat-storage-20261109/install_delivery.py').read_text()
s=s.replace('serve-chat-storage-20261109','serve-cache-io-20261109').replace('PASS_TARGETED_STORED_CHAT_DELIVERY','PASS_TARGETED_CACHE_PROGRESS_DELIVERY').replace('PASS_STORED_CHAT_DEFAULT_INSTALL','PASS_NATIVE_PROGRESS_CACHE_DEFAULT_INSTALL')
s=s.replace('c704eebf3d7f8f03fb566c5ab340277427a9304a09416bed0387676ed028f28f','92e600fbcdcadf37271a77405daf498f1fe4fbf2d327ad79a0bf3646bd02ce31')
s=s.replace('3216d5afdcea78b1f33e37a055c1acfea89eef88b22da2a2c4c75ef19c49c000','dfffcbadb6ba73c2184c06444f28d85979d2d0881d34aca7087bf1ff5fd31e75')
a=s.index("            scope='Accepted stored Chat");b=s.index('            control_review=',a)
s=s[:a]+'''            scope='Accepted native actual-completion progress, evidence-based recovery and observer-loss isolation; '
                'optional cache worker timeout with same-model recovery and target/MTP JSON/SSE. '
                'Previously accepted application protocols retained, all remaining master tasks continue.',
'''+s[b:]
s=s.replace('stored_chat_targeted_accepted=True,','native_progress_targeted_accepted=True, cache_io_targeted_accepted=True,\n            stored_chat_targeted_accepted=True,')
(base/'install_delivery.py').write_text(s)
