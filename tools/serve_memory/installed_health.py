"""Installed CLI/isolated worker with exact candidate HTTP evidence reuse."""
import os
import signal
import socket
import struct
import subprocess

from tools.serve_memory import stage


def run(plan, output):
    h, root = stage.h, stage.ROOT
    binary = root / "build/q4t"
    h.require(h.sha(binary) == plan["candidate_sha256"], "installed ELF changed")
    business = output / "business"
    h.require(h.read(business / "result.json")["status"] == "PASS_BOUNDED_MEMORY_ADMISSION", "HTTP proof missing")
    folder = business / "production-mtp"
    h.require(h.read(folder / "command.json")["binary"]["sha256"] == plan["candidate_sha256"],
              "HTTP proof used a different executable")
    proofs = {label: h.identity(folder / (label+"-result.json")) for label in
              ("ordinary-recovery", "project-read", "project-answer")}
    command = [str(binary), "version"]
    version = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
    h.require("Qwen4-Thor" in version.stdout, "installed CLI failed")
    parent, child = socket.socketpair()
    child_fd = child.fileno()
    actions = [(os.POSIX_SPAWN_DUP2, child_fd, 3)]
    if child_fd != 3:
        actions.append((os.POSIX_SPAWN_CLOSE, child_fd))
    pid = os.posix_spawn(command[0], [command[0], "--q4t-grammar-worker",
        str(os.getpid()), str(2*1024**3)], {"LC_ALL": "C"}, file_actions=actions)
    child.close()
    descriptor = None
    try:
        descriptor = os.pidfd_open(pid)
        parent.settimeout(5)
        raw = bytearray()
        while len(raw) < 24:
            part = parent.recv(24-len(raw))
            h.require(part, "worker not ready")
            raw.extend(part)
        magic, code, flags, words, rss = struct.unpack("=IIIIQ", raw)
        h.require((magic, code, flags, words) == (0x51475431, 0, 0, 0) and
                  0 < rss <= 2*1024**3, "installed worker handshake")
    finally:
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        if descriptor is not None:
            os.close(descriptor)
        parent.close()
    folder = output / "health"
    folder.mkdir()
    h.save(folder / "result.json", dict(status="PASS_SAME_ELF_HTTP_HEALTH_REUSED",
        binary=h.identity(binary), http_result=h.identity(business / "result.json"), http_proofs=proofs,
        installed_cli_stdout=version.stdout, worker_reported_rss_bytes=rss,
        new_model_requests=0, new_model_starts=0))
