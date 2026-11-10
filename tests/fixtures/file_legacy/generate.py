"""Regenerate original binary fixtures using a private system Office profile.

Trusted, checked-in flat ODF sources only. Does not load models or use GPU
acceleration. Generated output and profiles must stay in build/ or .q4t-work/.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    repo = source.parents[2]
    output = args.output.resolve()
    if not any(output.is_relative_to(repo / root) for root in ("build", ".q4t-work")):
        parser.error("output must be inside this checkout's build/ or .q4t-work/")
    output.mkdir(parents=True, exist_ok=False)
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC",
           "SAL_USE_VCLPLUGIN": "svp", "SAL_DISABLE_OPENCL": "1",
           "SAL_DISABLEGL": "1", "SAL_DISABLESKIA": "1", "MAX_CONCURRENCY": "1"}
    records = []
    for stem, extension, format_name in (
            ("word", "fodt", "doc:MS Word 97"),
            ("sheet", "fods", "xls:MS Excel 97"),
            ("slides", "fodp", "ppt:MS PowerPoint 97")):
        argv = ["/usr/bin/libreoffice", "-env:UserInstallation=" +
                (output / (stem + "-profile")).as_uri(), "--headless",
                "--nologo", "--nodefault", "--norestore", "--convert-to",
                format_name, "--outdir", str(output), str(source / (stem + "." + extension))]
        started = time.monotonic()
        with (output / (stem + ".stdout")).open("xb") as stdout, \
                (output / (stem + ".stderr")).open("xb") as stderr:
            process = subprocess.Popen(argv, env=env, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            try:
                code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
                raise
        target = output / (stem + "." + format_name.split(":")[0])
        if code or not target.is_file():
            raise RuntimeError(f"fixture conversion failed: {stem}; inspect saved logs")
        data = target.read_bytes()
        if not data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            raise RuntimeError("expected actual compound binary file")
        records.append(dict(argv=argv, exit_code=code, seconds=time.monotonic()-started,
                            name=target.name, bytes=len(data),
                            sha256=hashlib.sha256(data).hexdigest()))
    (output / "generation.json").write_text(json.dumps(records, indent=2) + "\n")
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
