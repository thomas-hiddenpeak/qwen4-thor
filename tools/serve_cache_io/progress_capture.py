"""Bounded private progress capture for validation; never directs model recovery."""
from dataclasses import asdict
from pathlib import Path
import socket
import struct
import threading
import time

from tools.serve_recovery import progress


class CaptureSocket:
    def __init__(self, sock):
        self.socket = sock
        self.packets = []
        self.bytes = 0

    def __getattr__(self, name):
        return getattr(self.socket, name)

    def recvmsg(self, *args):
        packet, ancillary, flags, address = self.socket.recvmsg(*args)
        if packet:
            if len(self.packets) >= 4096 or self.bytes + len(packet) > 8 * 1024**2:
                raise RuntimeError('bounded progress capture exhausted')
            self.packets.append(packet)
            self.bytes += len(packet)
        return packet, ancillary, flags, address


class Capture:
    def __init__(self, sock, identity):
        self.socket = CaptureSocket(sock)
        self.reader = progress.Reader(self.socket, identity)
        self.detector = progress.Detector()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.values = []
        self.unknown = {}
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        sequence = 0
        try:
            while not self.stop.wait(.025):
                value = self.reader.poll()
                if value is None:
                    reason = self.reader.reason
                    self.unknown[reason] = self.unknown.get(reason, 0) + 1
                if self.detector.assess(value) is not None:
                    raise RuntimeError('unexpected execution stall on healthy business')
                if value is not None and value.sequence != sequence:
                    with self.lock:
                        self.values.append(value)
                    sequence = value.sequence
        except BaseException as error:
            self.error = repr(error)

    def summary(self):
        with self.lock:
            rows = list(self.values)
        return dict(frames=len(self.socket.packets), bytes=self.socket.bytes,
            snapshots=len(rows), error=self.error,
            last=None if not rows else asdict(rows[-1]),
            cache_wait_samples=sum(any(w.wait == 9 for w in s.rows) for s in rows),
            cache_device_samples=sum(any(w.work == 4 and w.wait == 0 for w in s.rows)
                                     for s in rows),
            unknown=dict(self.unknown))

    def close(self, output):
        self.stop.set()
        self.thread.join(2)
        if self.thread.is_alive():
            raise RuntimeError('progress capture thread did not exit')
        self.reader.close()
        # Length-delimited unmodified native packets; contains no prompt/key.
        with Path(output).open('xb') as stream:
            for packet in self.socket.packets:
                stream.write(struct.pack('<I', len(packet)))
                stream.write(packet)
