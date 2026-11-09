"""Bounded PEM material validation and cached TLS validity observations."""
from contextlib import ExitStack
import datetime
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import ssl
import subprocess
import time

OPENSSL=Path('/usr/bin/openssl')
MAX_BYTES=128*1024
MAX_CHAIN=8
START_SECONDS=3600
WARN_SECONDS=7*24*3600


class Invalid(ValueError):pass


def require(value,code):
    if not value:raise Invalid(code)


def sha(raw):return hashlib.sha256(raw).hexdigest()


def command(args,raw=None,*,fds=(),code='gateway_tls_invalid'):
    try:
        result=subprocess.run([str(OPENSSL),*args],input=raw,capture_output=True,
            timeout=10,pass_fds=fds,env=dict(PATH='/usr/bin:/bin',LANG='C',LC_ALL='C',OPENSSL_CONF='/dev/null'))
        require(result.returncode==0 and len(result.stdout)+len(result.stderr)<=256*1024,code)
        return result.stdout
    except (OSError,subprocess.TimeoutExpired):raise Invalid('gateway_tls_validator_unavailable') from None


def certificates(raw,maximum=MAX_CHAIN):
    require(type(raw) is bytes and 0<len(raw)<=MAX_BYTES,'gateway_tls_material_size')
    pattern=rb'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----'
    blocks=re.findall(pattern,raw)
    require(1<=len(blocks)<=maximum and not re.sub(pattern,b'',raw).strip(),'gateway_tls_pem_invalid')
    result=[]
    for block in blocks:
        try:der=ssl.PEM_cert_to_DER_cert(block.decode('ascii'))
        except (ValueError,UnicodeError):raise Invalid('gateway_tls_pem_invalid') from None
        result.append((block+b'\n',sha(der)))
    require(len({v[1] for v in result})==len(result),'gateway_tls_duplicate_certificate')
    return result


def dates(block,digest):
    output=command(['x509','-noout','-dates','-dateopt','iso_8601'],block)
    fields={}
    try:
        for line in output.decode('ascii').splitlines():
            key,value=line.split('=',1)
            require(key in ('notBefore','notAfter') and key not in fields,'gateway_tls_dates_invalid')
            parsed=datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
            require(parsed.utcoffset()==datetime.timedelta(0),'gateway_tls_dates_invalid')
            fields[key]=int(parsed.timestamp())
        require(fields.keys()=={'notBefore','notAfter'} and fields['notBefore']<fields['notAfter'],
                'gateway_tls_dates_invalid')
    except (ValueError,UnicodeError,OverflowError):raise Invalid('gateway_tls_dates_invalid') from None
    return dict(sha256=digest,not_before=fields['notBefore'],not_after=fields['notAfter'])


def immutable_fd(stack,raw):
    fd=os.memfd_create('q4t-tls-input',os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
    stack.callback(os.close,fd)
    with os.fdopen(os.dup(fd),'wb') as stream:stream.write(raw)
    # Linux UAPI: Python built with older headers may omit these constants.
    # A missing Python name must not disable kernel immutability protection.
    fcntl.fcntl(fd,getattr(fcntl,'F_ADD_SEALS',1033),15)
    os.lseek(fd,0,os.SEEK_SET)
    return fd


def verify_chain(chain,anchor,hostname):
    with ExitStack() as stack:
        leaf=immutable_fd(stack,chain[0][0]);ca=immutable_fd(stack,anchor[0][0]);fds=[leaf,ca]
        args=['verify','-x509_strict','-purpose','sslserver','-verify_hostname',hostname,
              '-CAfile',f'/proc/self/fd/{ca}','-no-CApath','-no-CAstore']
        if len(chain)>1:
            intermediate=immutable_fd(stack,b''.join(v[0] for v in chain[1:]));fds.append(intermediate)
            args+=['-untrusted',f'/proc/self/fd/{intermediate}']
        command([*args,f'/proc/self/fd/{leaf}'],fds=tuple(fds),code='gateway_tls_chain_invalid')


def observe(metadata,now=None):
    now=time.time() if now is None else now
    try:
        require(type(now) in (int,float) and math.isfinite(now) and now>=0,'gateway_tls_clock_unavailable')
        start,end=metadata['not_before'],metadata['not_after']
        require(type(start) is int and type(end) is int and start<end,'gateway_tls_metadata_invalid')
        state,reason='VALID','none'
        if now<start:state,reason='NOT_YET_VALID','gateway_tls_not_yet_valid'
        elif now>=end:state,reason='EXPIRED','gateway_tls_expired'
        elif end-now<=WARN_SECONDS:state='EXPIRING'
        return dict(state=state,reason=reason,remaining_seconds=end-now,not_before=start,not_after=end,
            leaf_sha256=metadata['chain'][0]['sha256'],trust=metadata['trust'],observed_unix=now)
    except (ValueError,KeyError,TypeError,IndexError):
        return dict(state='UNKNOWN',reason='gateway_tls_observation_unavailable',remaining_seconds=None,
                    observed_unix=now if type(now) in (int,float) and math.isfinite(now) else None)


def admit(metadata,now=None):
    value=observe(metadata,now)
    require(value['state'] in ('VALID','EXPIRING'),value['reason'])
    require(value['remaining_seconds']>START_SECONDS,'gateway_tls_insufficient_lifetime')
    return value


def inspect(certificate,key,hostname,*,trusted_ca=None):
    # Local import avoids a module cycle; all reads are bounded/no-follow.
    from tools.serve_gateway import gateway as g
    raw=g.read_file(certificate,maximum=MAX_BYTES)
    key_raw=g.read_file(key,maximum=MAX_BYTES,private=True)
    chain=certificates(raw)
    anchor=None if trusted_ca is None else certificates(g.read_file(trusted_ca,maximum=MAX_BYTES),1)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    def no_password():raise Invalid('gateway_tls_encrypted_key_unsupported')
    try:
        with ExitStack() as stack:
            cert_fd=immutable_fd(stack,raw);key_fd=immutable_fd(stack,key_raw)
            context.load_cert_chain(f'/proc/self/fd/{cert_fd}',f'/proc/self/fd/{key_fd}',password=no_password)
    except (OSError,ssl.SSLError):raise Invalid('gateway_tls_key_pair_invalid') from None
    matched=command(['x509','-noout','-checkhost',hostname],chain[0][0],code='gateway_tls_hostname_invalid')
    require(matched.decode().strip()==f'Hostname {hostname} does match certificate','gateway_tls_hostname_invalid')
    purpose=command(['x509','-noout','-purpose'],chain[0][0])
    require(b'SSL server : Yes' in purpose.splitlines(),'gateway_tls_server_purpose_invalid')
    rows=[dates(*v) for v in chain];trust=None if anchor is None else dates(*anchor[0])
    all_dates=rows+([] if trust is None else [trust])
    metadata=dict(version=1,chain=rows,anchor=trust,trust='not_configured' if anchor is None else 'explicit_ca',
        not_before=max(v['not_before'] for v in all_dates),not_after=min(v['not_after'] for v in all_dates),
        openssl=dict(path=str(OPENSSL),sha256=sha(OPENSSL.read_bytes()),version=command(['version']).decode().strip()),
        python_ssl_version=ssl.OPENSSL_VERSION,revocation_checked=False)
    admit(metadata)
    if anchor is not None:
        ca_purpose=command(['x509','-noout','-purpose'],anchor[0][0])
        require(b'SSL server CA : Yes' in ca_purpose.splitlines(),'gateway_tls_anchor_invalid')
        verify_chain(chain,anchor,hostname)
    return metadata
