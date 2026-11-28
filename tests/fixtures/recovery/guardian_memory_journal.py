"""CPU fixture using the production journal with a controllable memory read."""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.serve_recovery import journal
from tools.serve_service import memory

directory = Path(sys.argv[sys.argv.index('--directory') + 1])
read = memory.read


def observed():
    if (directory/'memory.stall').exists():
        (directory/'memory.entered').touch()
        while (directory/'memory.stall').exists():
            time.sleep(.01)
        # A delayed read must not become a fresh proof when it completes.
        return dict(total_bytes=128 << 30, available_bytes=127 << 30)
    if (directory/'memory.error').exists():
        raise OSError('fixture observation failed')
    if (directory/'memory.low').exists():
        return dict(total_bytes=128 << 30, available_bytes=0)
    return read()


if __name__ == '__main__':
    memory.read = observed
    sys.exit(journal.main())
