// Development evidence only. Synchronous writes survive an actor process kill.
import fs from 'node:fs';

export function openJournal(file, maximumBytes = 32 << 20) {
  if (!Number.isSafeInteger(maximumBytes) || maximumBytes <= 0)
    throw new Error('journal byte limit');
  const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_CREAT |
    fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
  let used = 0, closed = false;
  return {
    append(direction, event) {
      if (closed || !['client', 'server'].includes(direction) ||
        !event || typeof event.type !== 'string') throw new Error('invalid journal event');
      const line = Buffer.from(JSON.stringify({ direction,
        monotonic_ns: process.hrtime.bigint().toString(), event }) + '\n');
      if (line.length > maximumBytes - used) throw new Error('journal byte bound');
      let at = 0;
      while (at < line.length) {
        const written = fs.writeSync(fd, line, at, line.length - at);
        if (!written) throw new Error('journal write did not progress');
        at += written;
      }
      used += line.length;
    },
    close() {
      if (!closed) { closed = true; fs.closeSync(fd); }
    },
  };
}
