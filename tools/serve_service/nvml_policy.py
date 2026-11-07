"""CPU-only interpretation of NVML observations, never a driver probe.

Values follow nvmlReturn_t in CUDA 13.3 nvml.h and the NVIDIA NVML API guide.
The retry/suspend/fatal choices are our serving policy, not recovery promises.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    category: str
    action: str
    nvml_code: int | None = None


DEVICE_FAILURE = frozenset({8, 11, 14, 15, 16, 28})
CONFIGURATION = frozenset({3, 4, 9, 12, 13, 17, 18, 22, 24, 25, 26, 30})
QUERY_ERROR = frozenset({1, 2, 5, 7, 29})


def error_decision(error):
    if (type(error) is not dict or type(error.get('type')) is not str or
            type(error.get('message')) is not str):
        return Decision('PROTOCOL_ERROR', 'suspend')
    code = error.get('nvml_code')
    if code is None:
        return Decision('CONFIGURATION' if error['type'] in
                        ('ImportError', 'ModuleNotFoundError') else 'QUERY_ERROR',
                        'suspend')
    if type(code) is not int or code <= 0:
        return Decision('PROTOCOL_ERROR', 'suspend')
    if code in DEVICE_FAILURE:
        return Decision('DEVICE_FAILURE', 'fatal', code)
    if code in CONFIGURATION:
        return Decision('CONFIGURATION', 'suspend', code)
    if code in QUERY_ERROR:
        return Decision('QUERY_ERROR', 'suspend', code)
    if code == 10:
        return Decision('NVML_TIMEOUT', 'retry', code)
    if code in (20, 23):
        return Decision('RESOURCE_UNAVAILABLE', 'retry', code)
    if code == 27:
        return Decision('NOT_READY', 'retry', code)
    # Includes UNKNOWN(999), NO_DATA, NOT_FOUND, IN_USE and future codes.
    # They are not proof of a temporary outage or an empty GPU.
    return Decision('NVML_UNCLASSIFIED', 'suspend', code)


def primary_decision(result):
    if type(result) is not dict:
        return Decision('PROTOCOL_ERROR', 'suspend')
    status = result.get('status')
    if status == 'OK':
        pids = result.get('pids')
        if (type(pids) is not list or any(type(p) is not int or p <= 0
                                        for p in pids) or
                result.get('error') is not None or
                result.get('shutdown_error') is not None):
            return Decision('PROTOCOL_ERROR', 'suspend')
        return Decision('OK', 'observe')
    if status == 'TIMEOUT':
        return Decision('QUERY_TIMEOUT', 'retry')
    if status == 'CLEANUP_FAILED':
        return Decision('WORKER_UNREAPED', 'suspend')
    if status != 'ERROR':
        return Decision('PROTOCOL_ERROR', 'suspend')
    decisions = [error_decision(result.get('error'))]
    if result.get('shutdown_error') is not None:
        decisions.append(error_decision(result['shutdown_error']))
    # A primary timeout must not hide a device failure during shutdown.
    return strongest(decisions)


def strongest(decisions):
    return max(decisions, key=lambda d: (
        {'observe': -1, 'retry': 0, 'suspend': 1, 'fatal': 2}[d.action],
        d.category in ('DEVICE_FAILURE', 'WORKER_UNREAPED'),
        d.nvml_code is not None))


def classify(result):
    primary = primary_decision(result)
    if type(result) is not dict:
        return primary
    observed = result.get('observed_errors', [])
    decisions = [primary]
    # A collector's final status can be inconsistent. Preserve a complete
    # explicit device error even when its enclosing status is malformed.
    decisions.extend(error_decision(result[field])
                     for field in ('error', 'shutdown_error')
                     if result.get(field) is not None)
    if (result.get('protocol_error') is not None or type(observed) is not list or
            len(observed) > 2 or (primary.category == 'OK' and observed)):
        decisions.append(Decision('PROTOCOL_ERROR', 'suspend'))
    if type(observed) is list and len(observed) <= 2:
        decisions.extend(error_decision(error) for error in observed)
    # External timeout/unreaped status must not hide an already reported device
    # error when shutdown stalls before the final worker record is emitted.
    return strongest(decisions)
