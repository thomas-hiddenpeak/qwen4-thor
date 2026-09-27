"""Check actual listen sockets and CLI failures against a full runner binary."""
import argparse
import hashlib
from http.client import HTTPConnection
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--binary', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--model-dir', required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / x) for x in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out)
    save = lambda name, d: (out / name).write_text(json.dumps(d, indent=2))
    interfaces = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr']))
    save('interfaces.json', interfaces)
    external = next(x['local'] for i in interfaces for x in i['addr_info'] if x['scope'] == 'global' and not ipaddress.ip_address(x['local']).is_loopback)
    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    binary = str(args.binary.resolve())
    records = []
    save('plan.json', {'binary_sha256': hashlib.sha256(Path(binary).read_bytes()).hexdigest(), 'external_local_interface': external})
    for i, flags in enumerate([['--host', 'localhost'], ['--host', '::1'], ['--host', '999.1.1.1'], ['--host', ''], ['--port', '0'], ['--port', '65536'], ['--host']]):
        cmd = [binary, 'serve', '--model-dir', '/nonexistent-listen-test', *flags]
        r = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        (out / f'invalid-{i}.log').write_bytes(r.stdout)
        assert r.returncode in (1, 2) and b'tokenizer' not in r.stdout
        assert b'numeric IPv4' in r.stdout or b'port must be' in r.stdout or b'Unknown option' in r.stdout
        records.append({'command': cmd, 'exit': r.returncode})
    for label, flags, expected, connects, rejects in [
        ('default', [], '127.0.0.1', ['127.0.0.1'], [external]),
        ('wildcard', ['--host', '0.0.0.0'], '0.0.0.0', ['127.0.0.1', external], []),
        ('interface', ['--host', external], external, [external], ['127.0.0.1']),
    ]:
        cmd = [binary, 'serve', '--model-dir', args.model_dir, '--port', '8000', '--max-seq', '1', '--max-prefill', '1024', '--max-len', '8192', '--no-mtp', *flags]
        save(label + '-command.json', cmd)
        with (out / (label + '.log')).open('w') as log:
            server = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                end = time.monotonic() + 180
                while 'serving on port' not in (out / (label + '.log')).read_text():
                    assert server.poll() is None and time.monotonic() < end
                    time.sleep(.2)
                table = Path('/proc/net/tcp').read_text()
                (out / (label + '-tcp.txt')).write_text(table)
                inode = {os.readlink(f) for f in Path(f'/proc/{server.pid}/fd').iterdir()}
                listeners = [line.split()[1] for line in table.splitlines()[1:] if line.split()[3] == '0A' and 'socket:[' + line.split()[9] + ']' in inode]
                expected_hex = socket.inet_aton(expected)[::-1].hex().upper() + ':1F40'
                assert listeners == [expected_hex], listeners
                for host in connects:
                    c = HTTPConnection(host, 8000, timeout=5)
                    try:
                        c.request('GET', '/healthz')
                        r = c.getresponse()
                        assert r.status == 200 and json.loads(r.read())['gpu_healthy']
                    finally:
                        c.close()
                for host in rejects:
                    try:
                        s = socket.create_connection((host, 8000), timeout=2)
                    except OSError:
                        pass
                    else:
                        s.close()
                        raise AssertionError('unexpected access: ' + host)
                records.append({'case': label, 'listener': listeners, 'connects': connects, 'rejects': rejects})
            finally:
                server.terminate()
                server.wait(timeout=60)
                save(label + '-exit.json', {'server': server.returncode})
            assert server.returncode == 0
            assert 'shutdown complete (0 in-flight remaining)' in (out / (label + '.log')).read_text()
    save('results.json', records)
    print('listen address checks passed', flush=True)


if __name__ == '__main__':
    main()
