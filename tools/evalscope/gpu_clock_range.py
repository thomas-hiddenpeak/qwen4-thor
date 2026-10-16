"""Diagnostic GPC range transaction primitive, with injected IO.

This is NOT a command-line clock controller. A caller must hold the GPU lease,
provide privilege-separated IO, and arrange restoration after termination before
using real sysfs. Only min/max change; governor/other clocks stay untouched.
"""


def steps(current, target):
    """Plan writes that never intentionally set min above max."""
    low, high = current
    new_low, new_high = target
    if not 0 < low <= high or not 0 < new_low <= new_high:
        raise ValueError('invalid clock range')
    order = ('min_freq', 'max_freq') if new_high < low else ('max_freq', 'min_freq')
    values = {'min_freq': low, 'max_freq': high}
    targets = {'min_freq': new_low, 'max_freq': new_high}
    result = []
    for name in order:
        if values[name] != targets[name]:
            values[name] = targets[name]
            assert values['min_freq'] <= values['max_freq']
            result.append((name, values[name]))
    return result


class ClockRange:
    def __init__(self, read, write, expected, available):
        self.read = read
        self.write = write
        self.expected = tuple(expected)
        self.available = set(available)
        self.history = []
        self.owned = tuple(read())
        if self.owned != self.expected:
            raise RuntimeError('initial clock range differs from frozen identity')
        steps(self.expected, self.expected)  # Validate range even with no writes.

    def apply(self, target):
        target = tuple(target)
        if any(x not in self.available for x in target):
            raise ValueError('unsupported frequency')
        if target[1] > self.expected[1]:
            raise ValueError('diagnostic cannot exceed original maximum')
        current = tuple(self.read())
        if current != self.owned:
            raise RuntimeError('clock range changed outside transaction')
        for name, value in steps(current, target):
            predicted = (value, current[1]) if name == 'min_freq' else (current[0], value)
            try:
                self.write(name, value)
            except BaseException:
                observed = tuple(self.read())
                # A write can take effect before the syscall reports failure.
                if observed in (current, predicted):
                    self.owned = observed
                self.history.append(dict(field=name, value=value,
                                         observed=observed, write_failed=True))
                raise
            observed = tuple(self.read())
            self.history.append(dict(field=name, value=value, observed=observed))
            if observed != predicted:
                if observed == current:
                    self.owned = observed
                raise RuntimeError('clock range readback mismatch')
            current = self.owned = observed
        if tuple(self.read()) != target:
            raise RuntimeError('clock range changed after writes')

    def restore(self):
        self.apply(self.expected)
        if tuple(self.read()) != self.expected:
            raise RuntimeError('original clock range not restored')
