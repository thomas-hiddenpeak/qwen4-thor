"""Pinned Prometheus/Alertmanager webhook package; explicit local activation."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_gateway import managed as m
from tools.serve_service import package as pkg, supervisor as sup
from tools.serve_ops import identity

require = pkg.require
SOURCES = (*m.SOURCES, 'tools/serve_notify/notify.py',
           'tools/serve_notify/child.py', 'deploy/serve-ops/q4t.rules.yml')
BINARIES = {
    'prometheus': 'aeebc0ffc6529c87221ab12ff1f9fc276be488869ebc942ce3e4d7ffcfe6a97a',
    'promtool': 'd4ff433304144fae3d4490bd381be983e99064c601bc35e8dfe29f52300a9bc4',
    'alertmanager': '62198848d4d81586fa043a1b555dcccdacfce00a5120f3cfdb2f35757676c1c7',
    'amtool': '6c331abac6c7034ad75ab8139b5d432357c80391c68c245cf8dbadcd5abaed94',
}
ROLES = ('prometheus', 'alertmanager')
SETTINGS = dict(poll_seconds=1, startup_seconds=30, stop_seconds=15,
                kill_seconds=5, log_bytes=4*1024**2, restarts=0)


def policy(value):
    required = {'version', 'deployment', 'runner_port', 'prometheus_port',
                'alertmanager_port', 'receiver_url', 'receiver_token_file'}
    require(required <= value.keys() <= required | {'receiver_ca_file'},
            'unknown or missing notification policy field')
    require(type(value['version']) is int and value['version'] == 1,
            'unknown notification policy')
    require(isinstance(value['deployment'], str) and
            re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', value['deployment']),
            'invalid deployment label')
    ports = [value[n] for n in ('runner_port', 'prometheus_port',
                                'alertmanager_port')]
    require(all(type(v) is int and 1024 <= v <= 65535 for v in ports) and
            len(set(ports)) == 3, 'invalid or overlapping ports')
    url = value['receiver_url']
    require(isinstance(url, str) and len(url) <= 2048 and
            re.fullmatch(r'[\x21-\x7e]+', url), 'invalid receiver URL')
    parsed = urlsplit(url)
    require(parsed.scheme == 'https' and parsed.hostname and
            not parsed.username and not parsed.password and
            not parsed.query and not parsed.fragment and parsed.path and
            '\\' not in url, 'HTTPS webhook without URL credentials required')
    require(parsed.port is None or 1 <= parsed.port <= 65535, 'receiver port')
    require(all(isinstance(value.get(n, ''), str) for n in
                ('receiver_token_file', 'receiver_ca_file')), 'file path type')
    return dict(value)


def rules():
    definitions = (
        ('Q4TNotifyHeartbeat', 'vector(1)', '', 'information',
         'Monitoring heartbeat; the receiver must detect missing heartbeats.'),
        ('Q4TAlertmanagerDown', 'up{job="q4t-alertmanager"} == 0', '30s',
         'critical', 'The local notification dispatcher is unreachable.'),
        ('Q4TNotificationFailures',
         'increase(alertmanager_notifications_failed_total{job="q4t-alertmanager",integration="webhook"}[5m]) > 0',
         '', 'warning', 'Webhook delivery failures occurred in the last five minutes.'),
        ('Q4TNotificationQueueDropped',
         'increase(prometheus_notifications_dropped_total{job="q4t-monitor"}[5m]) > 0',
         '', 'critical', 'Prometheus dropped queued notifications.'),
        ('Q4TNotificationSendErrors',
         'increase(prometheus_notifications_errors_total{job="q4t-monitor"}[5m]) > 0',
         '', 'warning', 'Prometheus could not send some alerts to Alertmanager.'),
    )
    rows = []
    for name, expr, duration, severity, summary in definitions:
        rows.append(dict(alert=name, expr=expr,
            **({'for': duration} if duration else {}),
            labels=dict(severity=severity, component='q4t-monitor'),
            annotations=dict(summary=summary)))
    return dict(groups=[dict(name='q4t-notification', interval='15s', rules=rows)])


def configs(p, output):
    output = Path(output)
    http = dict(follow_redirects=False, enable_http2=False,
                proxy_from_environment=False,
                authorization=dict(type='Bearer',
                    credentials_file=str(output/'token-private')),
                tls_config=dict(min_version='TLS12', insecure_skip_verify=False))
    if p.get('receiver_ca_file'):
        http['tls_config']['ca_file'] = str(output/'receiver-ca.pem')
    alertmanager = dict(
        route=dict(receiver='q4t-webhook',
            group_by=['deployment', 'alertname', 'job', 'instance'],
            group_wait='5s', group_interval='10s', repeat_interval='4h',
            routes=[dict(matchers=['alertname="Q4TNotifyHeartbeat"'],
                receiver='q4t-webhook', group_wait='0s',
                group_interval='1m', repeat_interval='1m')]),
        receivers=[dict(name='q4t-webhook', webhook_configs=[dict(
            url_file=str(output/'url-private'), send_resolved=True,
            max_alerts=0, timeout='5s', http_config=http)])])
    jobs = []
    for name, port, sample_limit in (
            ('q4t', p['runner_port'], 512),
            ('q4t-alertmanager', p['alertmanager_port'], 2048),
            ('q4t-monitor', p['prometheus_port'], 8192)):
        jobs.append(dict(job_name=name, honor_labels=False,
            follow_redirects=False, enable_http2=False,
            proxy_from_environment=False, body_size_limit='4MB',
            sample_limit=sample_limit, label_limit=32,
            label_name_length_limit=128, label_value_length_limit=1024,
            static_configs=[dict(targets=[f'127.0.0.1:{port}'])]))
    prometheus = dict(
        global_=dict(scrape_interval='15s', scrape_timeout='2s',
                     evaluation_interval='15s',
                     external_labels=dict(deployment=p['deployment'])),
        rule_files=[str(output/'source/deploy/serve-ops/q4t.rules.yml'),
                    str(output/'notify.rules.json')], scrape_configs=jobs,
        alerting=dict(alertmanagers=[dict(scheme='http', api_version='v2',
            timeout='2s', follow_redirects=False, proxy_from_environment=False,
            static_configs=[dict(targets=[f"127.0.0.1:{p['alertmanager_port']}"])])],
            alert_relabel_configs=[dict(action='labelkeep',
                regex='alertname|severity|component|deployment|job|instance')]))
    prometheus['global'] = prometheus.pop('global_')
    return dict(prometheus=prometheus, alertmanager=alertmanager)


def prepare(policy_path, binaries, output):
    p = policy(m.read(policy_path))
    token = m.g.read_file(p['receiver_token_file'], private=True, maximum=1024)
    require(re.fullmatch(rb'[A-Za-z0-9_~+/.=-]{32,512}\n?', token),
            'private bearer credential required')
    for name, path in binaries.items():
        require(name in BINARIES and pkg.sha(path) == BINARIES[name],
                'unsupported dependency identity: '+name)
    require(binaries.keys() == BINARIES.keys(), 'missing dependency')
    target = pkg.private_directory(output)
    for name, path in binaries.items():
        data = m.g.read_file(path, maximum=256*1024**2)
        require(hashlib.sha256(data).hexdigest() == BINARIES[name],
                'dependency changed while copying')
        m.g.save(target/name, data); (target/name).chmod(0o500)
    for name in SOURCES:
        path = target/'source'/name
        path.parent.mkdir(parents=True, exist_ok=True)
        m.g.save(path, m.g.read_file(ROOT/name, maximum=2*1024**2))
        path.chmod(0o400)
    m.g.save(target/'token-private', token)
    m.g.save(target/'url-private', p['receiver_url'].encode())
    if p.get('receiver_ca_file'):
        m.g.save(target/'receiver-ca.pem', m.g.read_file(
            p['receiver_ca_file'], maximum=1024**2))
    pkg.save(target/'notify.rules.json', rules())
    for role, config in configs(p, target).items():
        pkg.save(target/(role+'.json'), config)
    checks = ([target/'promtool', 'check', 'config', target/'prometheus.json'],
              [target/'amtool', 'check-config', target/'alertmanager.json'])
    for index, argv in enumerate(checks):
        result = subprocess.run(list(map(str, argv)), capture_output=True,
                                timeout=20, env=dict(PATH='/usr/bin:/bin'))
        m.g.save(target/f'validation-{index}-private.log',
                 result.stdout+result.stderr)
        require(result.returncode == 0, 'configuration validation failed')
    files = [str(path.relative_to(target)) for path in target.rglob('*')
             if path.is_file() and not path.name.endswith('-private.log')]
    python = Path(sys.executable).resolve()
    manifest = dict(version=1, repository=str(ROOT), settings=SETTINGS,
        ports={n:p[n+'_port'] for n in (*ROLES, 'runner')},
        python=dict(path=str(python), sha256=pkg.sha(python)),
        files={name:pkg.sha(target/name) for name in sorted(files)},
        dependency_versions=dict(prometheus='3.5.0', alertmanager='0.34.2'),
        automatic_start=False, external_receiver_accepted=False)
    pkg.save(target/'manifest.json', manifest)
    digest = pkg.sha(target/'manifest.json')
    verify(target, digest)
    return dict(package=str(target), manifest_sha256=digest,
                automatic_start=False, external_receiver_accepted=False)


def verify(package, digest):
    package = m.private(Path(package).resolve(strict=True))
    require(pkg.sha(package/'manifest.json') == digest, 'manifest changed')
    value = m.read(package/'manifest.json')
    expected = {*BINARIES, *('source/'+s for s in SOURCES),
                'prometheus.json', 'alertmanager.json', 'notify.rules.json',
                'url-private', 'token-private'}
    require(value['version'] == 1 and value['settings'] == SETTINGS and
            set(value['files']) in (expected, expected | {'receiver-ca.pem'}),
            'unknown notification package contract')
    for name, wanted in value['files'].items():
        raw = m.g.read_file(package/name, private=True, maximum=256*1024**2)
        require(hashlib.sha256(raw).hexdigest() == wanted,
                'package file changed: '+name)
    for name, digest in BINARIES.items():
        require(value['files'][name] == digest, 'dependency identity changed')
    require(pkg.sha(value['python']['path']) == value['python']['sha256'],
            'Python changed')
    return value


def init_state(package, digest, output):
    verify(package, digest)
    path = pkg.private_directory(output)
    for role in ROLES: (path/role).mkdir(mode=0o700)
    pkg.save(path/'binding.json', dict(package=str(Path(package).resolve()),
                                     manifest_sha256=digest))
    return dict(state=str(path), new=True)


def check_state(path, package, digest):
    path = m.private(Path(path).resolve(strict=True))
    require(m.read(path/'binding.json') == dict(
        package=str(Path(package).resolve()), manifest_sha256=digest),
        'persistent state belongs to another package')
    for role in ROLES: m.private(path/role)
    return path


@contextmanager
def claim(state, role):
    require(role in ROLES, 'unknown role')
    fd = os.open(Path(state)/(role+'.lock'),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                info.st_mode & 0o077 == 0, 'invalid notification state claim')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


def command(package, manifest, role, state):
    require(role in ROLES, 'unknown role')
    prefix = str(Path(package)/role)
    common = [prefix, '--config.file='+str(Path(package)/(role+'.json')),
              '--web.listen-address=127.0.0.1:'+str(manifest['ports'][role]),
              '--log.format=json', '--log.level=info']
    if role == 'alertmanager':
        return common + ['--storage.path='+str(Path(state)/role),
            '--cluster.listen-address=', '--data.retention=120h',
            '--silences.max-silences=100', '--silences.max-silence-size-bytes=4096',
            '--alerts.per-alertname-limit=32', '--web.get-concurrency=8',
            '--web.timeout=5s']
    return common + ['--storage.tsdb.path='+str(Path(state)/role),
        '--storage.tsdb.retention.time=24h', '--storage.tsdb.retention.size=64MB',
        '--query.max-concurrency=4', '--query.max-samples=100000',
        '--query.timeout=5s', '--alertmanager.notification-queue-capacity=100',
        '--rules.alert.resend-delay=15s']


def observe(owner, port, binary):
    require(m.alive(owner), 'monitor process identity changed')
    proc = Path('/proc')/str(owner['pid'])
    actual, expected = (proc/'exe').stat(), Path(binary).stat()
    require((actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino),
            'monitor executable differs')
    own, child = Path('/proc/self/ns/net').stat(), (proc/'ns/net').stat()
    require((own.st_dev, own.st_ino) == (child.st_dev, child.st_ino),
            'monitor network namespace changed')
    fds = list(itertools.islice((proc/'fd').iterdir(), 4097))
    require(len(fds) <= 4096, 'monitor descriptor observation bound')
    owned = set()
    for fd in fds:
        try:
            value = os.readlink(fd)
            if value.startswith('socket:['): owned.add(value[8:-1])
        except FileNotFoundError:
            pass
    result = identity.listener(identity.bounded_file(
        Path('/proc/net/tcp')).decode(), port, owned)
    require(result['listener_scope'] == 'loopback' and m.alive(owner),
            'monitor listener is not owned loopback')
    return dict(result, owner=owner)


def run(package, digest, role, state, output):
    manifest = verify(package, digest)
    state = check_state(state, package, digest)
    output = pkg.private_directory(output, Path(manifest['repository']))
    owner = sup.process_identity(os.getpid())
    control = dict(owner=owner, package=str(Path(package).resolve()),
        manifest_sha256=digest, role=role, persistent_state=str(state))
    pkg.save(output/'controller.json', control)
    stopping = threading.Event()
    previous = {s:signal.signal(s, lambda *_:stopping.set())
                for s in (signal.SIGTERM, signal.SIGINT)}
    process = logger = None
    failure = None
    clean = True
    try:
        with claim(state, role) as fd, sup.processes.child_subreaper():
            control['claim_identity'] = [os.fstat(fd).st_dev, os.fstat(fd).st_ino]
            pkg.save(output/'controller.json', control)
            try:
                argv = [manifest['python']['path'], '-I', '-B', str(
                    Path(package)/'source/tools/serve_notify/child.py'),
                    '--run', str(output)]
                env = dict(PATH='/usr/bin:/bin', LANG='C.UTF-8',
                    HOME=str(Path.home()), Q4T_NOTIFY_CLAIM_FD=str(fd),
                    GOMAXPROCS='2',
                    GOMEMLIMIT='256MiB' if role == 'prometheus' else '128MiB')
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=True, pass_fds=(fd,), env=env, umask=0o077)
                clean = False
                child = sup.process_identity(process.pid)
                logger = sup.RotatingLog(output/'service.log', SETTINGS['log_bytes'])
                logger.start(process.stdout)
                started, first = time.monotonic(), None
                while not stopping.is_set() and not (output/'stop.request.json').exists():
                    require(process.poll() is None, 'monitor exited unexpectedly')
                    require(logger.error is None, 'monitor log capture failed')
                    for name, wanted in manifest['files'].items():
                        if '/' not in name and name not in BINARIES:
                            require(pkg.sha(Path(package)/name) == wanted,
                                    'monitor input changed')
                    try:
                        current = observe(child, manifest['ports'][role],
                                          Path(package)/role)
                    except (OSError, RuntimeError, identity.InvalidObservation):
                        if first or time.monotonic()-started >= SETTINGS['startup_seconds']:
                            raise
                    else:
                        require(first is None or current == first, 'listener changed')
                        first = current
                        pkg.save(output/'status.json', dict(state='LISTENING',
                            child=child, listener=current, updated_unix=time.time()))
                    stopping.wait(SETTINGS['poll_seconds'])
            except BaseException as error:
                failure = repr(error)
            finally:
                if process is not None:
                    sup.processes.stop_children({'monitor':process},
                        {'monitor':None if logger is None else logger.thread},
                        grace=SETTINGS['stop_seconds'], kill_grace=SETTINGS['kill_seconds'])
                    clean = True
                    require(logger is not None and logger.error is None,
                            'monitor log capture failed')
    except BaseException as error:
        failure = failure or repr(error)
    finally:
        for sig, handler in previous.items(): signal.signal(sig, handler)
        pkg.save(output/'terminal.json', dict(state='FAILED' if failure else 'STOPPED',
            error=failure, cleanup_confirmed=clean, starts=int(process is not None),
            restarts=0, child_returncode=None if process is None else process.returncode,
            automatic_reactivation=False, model_signalled=False))
    return 1 if failure else 0


def unit(package, digest, role, state, output):
    manifest = verify(package, digest)
    check_state(state, package, digest)
    def quote(value):
        value = str(value)
        require(not any(c in value for c in '\n\r\x00'), 'invalid unit argument')
        return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'
    argv = [manifest['python']['path'], '-I', '-B', Path(package).resolve()/
        'source/tools/serve_notify/notify.py', 'run', '--package', Path(package).resolve(),
        '--manifest-sha256', digest, '--role', role, '--state', Path(state).resolve(),
        '--output', Path(output).absolute()]
    return ('[Unit]\nDescription=Qwen4 Thor explicit notification component\n'
        '[Service]\nType=exec\nExecStart='+' '.join(map(quote,argv))+'\n'
        'Restart=no\nKillMode=mixed\nTimeoutStopSec=30\nUMask=0077\n'
        'NoNewPrivileges=yes\nLimitCORE=0\nStandardOutput=journal\nStandardError=journal\n'
        'UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME\n'
        '# No automatic activation or model dependency.\n')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare')
    for name in ('policy', 'output', *BINARIES): p.add_argument('--'+name, type=Path, required=True)
    for action in ('check', 'init-state', 'run', 'unit'):
        p = sub.add_parser(action)
        p.add_argument('--package', type=Path, required=True)
        p.add_argument('--manifest-sha256', required=True)
        if action != 'check': p.add_argument('--output', type=Path, required=True)
        if action in ('run', 'unit'):
            p.add_argument('--role', choices=ROLES, required=True)
            p.add_argument('--state', type=Path, required=True)
    for action in ('status', 'stop'):
        p = sub.add_parser(action); p.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        value = prepare(args.policy, {n:getattr(args, n) for n in BINARIES}, args.output)
    elif args.action == 'check':
        verify(args.package, args.manifest_sha256); value = dict(verified=True)
    elif args.action == 'init-state':
        value = init_state(args.package, args.manifest_sha256, args.output)
    elif args.action in ('status', 'stop'):
        value = (m.status if args.action == 'status' else m.stop)(args.run)
    else:
        manifest = verify(args.package, args.manifest_sha256)
        entry = args.package.resolve()/'source/tools/serve_notify/notify.py'
        if args.action == 'unit':
            print(unit(args.package, args.manifest_sha256, args.role, args.state, args.output), end='')
            return 0
        if Path(__file__).resolve() != entry:
            argv = [manifest['python']['path'], '-I', '-B', str(entry), 'run',
                '--package', str(args.package.resolve()), '--manifest-sha256', args.manifest_sha256,
                '--role', args.role, '--state', str(args.state.resolve()),
                '--output', str(args.output.absolute())]
            os.execve(argv[0], argv, dict(PATH='/usr/bin:/bin', LANG='C.UTF-8', HOME=str(Path.home())))
        return run(args.package, args.manifest_sha256, args.role, args.state, args.output)
    print(json.dumps(value)); return 0


if __name__ == '__main__':
    raise SystemExit(main())
