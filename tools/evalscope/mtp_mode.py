"""Resolve new execution modes without reinterpreting historical evidence.

The evidence readers retain their legacy T4 defaults. New launchers explicitly
pin the resolved verifier, so a source fixture or a server default cannot
silently change which computation a run is checking.
"""

VERIFIERS = ('t4', 'sequential')


def resolve_new_run_verifier(mtp, requested=None):
    if requested is not None and requested not in VERIFIERS:
        raise ValueError('invalid MTP verifier')
    if requested is not None and not mtp:
        raise ValueError('--mtp-verifier requires --mtp')
    # Plain evidence readers historically receive 't4' as their unused mode
    # argument. Launchers still serialize 'none' and send no verifier option.
    return (requested or 'sequential') if mtp else 't4'


def server_mtp_args(mtp, verifier):
    if verifier not in VERIFIERS or (not mtp and verifier != 't4'):
        raise ValueError('invalid resolved MTP mode')
    return ['--mtp', '--mtp-verifier', verifier] if mtp else ['--no-mtp']


def cancellation_server_mode(command, scope, requested=None):
    """Return a copied command and the verifier its scope actually checks.

    Full/decode-recovery keep historical T4 semantics for omitted source
    verifiers. Same-mode uses source files only as fixtures/capacity and gets
    the new sequential default, independently of the source verifier.
    """
    if scope not in ('full', 'decode-recovery', 'same-mode-recovery'):
        raise ValueError('invalid cancellation scope')
    if requested is not None and scope != 'same-mode-recovery':
        raise ValueError('--mtp-verifier requires same-mode-recovery')
    if requested is not None and requested not in VERIFIERS:
        raise ValueError('invalid MTP verifier')
    result = list(command)
    if (result.count('--mtp') + result.count('--no-mtp') != 1 or
            any(arg.startswith(('--mtp=', '--no-mtp=', '--mtp-verifier='))
                for arg in result)):
        raise ValueError('ambiguous source MTP mode')
    if result.count('--mtp-verifier') > 1:
        raise ValueError('ambiguous source verifier')
    source_mtp = '--mtp' in result
    source_verifier = 't4'
    if '--mtp-verifier' in result:
        index = result.index('--mtp-verifier')
        if index + 1 >= len(result) or result[index + 1] not in VERIFIERS:
            raise ValueError('invalid source verifier')
        if not source_mtp:
            raise ValueError('plain source command has an MTP verifier')
        source_verifier = result[index + 1]
        del result[index:index + 2]
    if scope == 'decode-recovery' and source_mtp:
        raise ValueError('decode-recovery requires an ordinary source')
    mtp = source_mtp if scope == 'full' else True
    verifier = (resolve_new_run_verifier(True, requested)
                if scope == 'same-mode-recovery' else source_verifier)
    index = result.index('--mtp' if source_mtp else '--no-mtp')
    result[index:index + 1] = server_mtp_args(mtp, verifier)
    return result, verifier
