"""Private generation ownership, admission reservations and explicit retention.

Only fresh stores and their own sealed generations are eligible for deletion.
Reservations bound admission, not a filesystem quota or total machine storage.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import time

from tools.serve_service import package as pkg

ROOT=Path(__file__).resolve().parents[2]
FORMAT='q4t.serve.retention.v1'
RESERVATIONS={'backend':96*1024**2,'gateway':32*1024**2}
META_LIMIT=256*1024
MAX_FILES=128
NAME=re.compile(r'g-[0-9a-f]{32}\Z')
require=pkg.require


def identity(info):
    return [info.st_dev,info.st_ino]


def private(path,directory=False):
    value=Path(path).lstat()
    require((stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)) and
            value.st_uid==os.geteuid() and value.st_mode&0o077==0,'private owned object required')
    if not directory:require(value.st_nlink==1,'hardlinked store file')
    return value


def read(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and
                info.st_mode&0o077==0 and info.st_nlink==1 and info.st_size<=META_LIMIT,
                'invalid private metadata')
        raw=os.read(fd,META_LIMIT+1)
        require(len(raw)<=META_LIMIT,'metadata budget')
        def pairs(items):
            result={}
            for name,value in items:
                require(name not in result,'duplicate metadata field');result[name]=value
            return result
        return json.loads(raw,object_pairs_hook=pairs,
            parse_constant=lambda _:require(False,'non-finite metadata'))
    finally:os.close(fd)


def save(path,value):
    require(len(json.dumps(value,ensure_ascii=True).encode())<META_LIMIT//2,'metadata budget')
    pkg.save(path,value)


def init(root,*,max_bytes=256*1024**2,max_generations=16,max_age_days=14):
    require(type(max_bytes) is int and 128*1024**2<=max_bytes<=2**40 and
            type(max_generations) is int and 1<=max_generations<=256 and
            type(max_age_days) is int and 1<=max_age_days<=3650,'invalid retention policy')
    root=Path(root).absolute()
    require(root==root.resolve() and any(root.is_relative_to(ROOT/n) for n in ('.q4t-work','build')),
            'fresh workspace store required')
    root.parent.mkdir(parents=True,exist_ok=True)
    root.mkdir(mode=0o700)
    (root/'generations').mkdir(mode=0o700)
    for path in (root/'.lock',):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    save(root/'store.json',dict(format=FORMAT,store_id=secrets.token_hex(32),
        root_identity=identity(root.stat()),generations_identity=identity((root/'generations').stat()),
        policy=dict(max_bytes=max_bytes,max_generations=max_generations,max_age_days=max_age_days),
        reservations=RESERVATIONS,automatic_pruning=False,created_unix=time.time()))
    return root


@contextmanager
def locked(root):
    root=Path(root).absolute()
    require(root==root.resolve(),'store path contains symlink')
    info=private(root,True);private(root/'generations',True);private(root/'.lock')
    fd=os.open(root/'.lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        meta=read(root/'store.json')
        require(meta['format']==FORMAT and meta['root_identity']==identity(info) and
                meta['generations_identity']==identity((root/'generations').stat()) and
                meta['reservations']==RESERVATIONS and meta['automatic_pruning'] is False,
                'store identity/contract changed')
        policy=meta['policy']
        require(set(policy)=={'max_bytes','max_generations','max_age_days'} and
                type(policy['max_bytes']) is int and 128*1024**2<=policy['max_bytes']<=2**40 and
                type(policy['max_generations']) is int and 1<=policy['max_generations']<=256 and
                type(policy['max_age_days']) is int and 1<=policy['max_age_days']<=3650,'invalid store policy')
        require(set(os.listdir(root)) <= {'store.json','.lock','generations','gc.json','last-gc.json'},
                'unexpected store entries')
        yield root,meta
    finally:os.close(fd)


@contextmanager
def claim(folder):
    private(folder,True);private(Path(folder)/'.lease')
    fd=os.open(Path(folder)/'.lease',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield fd
    finally:os.close(fd)


def busy(folder):
    try:
        with claim(folder):return False
    except BlockingIOError:return True


def tree(folder,limit,*,hashes=True):
    """Reject unknown object types, symlinks and hardlinks before any deletion."""
    files={};directories={'.':identity(private(folder,True))};total=0
    def visit(parent,depth):
        nonlocal total
        require(depth<=3,'generation nesting limit')
        entries=list(parent.iterdir())
        require(len(entries)+len(files)+len(directories)<=MAX_FILES,'generation entry budget')
        for path in sorted(entries):
            name=str(path.relative_to(folder))
            require(len(name)<=256 and '\n' not in name,'invalid generation path')
            try:info=path.lstat()
            except FileNotFoundError:
                if not hashes:continue
                raise
            if stat.S_ISDIR(info.st_mode):
                directories[name]=identity(private(path,True));visit(path,depth+1)
            else:
                try:private(path)
                except FileNotFoundError:
                    if not hashes:continue
                    raise
                if name in ('.lease','seal.json'):continue
                total+=info.st_size
                require(total<=limit,'generation exceeded reserved byte budget')
                row=dict(identity=identity(info),bytes=info.st_size,mtime_ns=info.st_mtime_ns)
                if hashes:
                    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                    with os.fdopen(fd,'rb') as stream:
                        require(identity(os.fstat(stream.fileno()))==row['identity'],'file replaced during read')
                        row['sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
                        require(os.fstat(stream.fileno()).st_size==row['bytes'] and
                                os.fstat(stream.fileno()).st_mtime_ns==row['mtime_ns'],'file changed during read')
                files[name]=row
    visit(Path(folder),0)
    return dict(files=files,directories=directories,bytes=total)


def reservation(folder,meta):
    require(NAME.fullmatch(folder.name),'unknown generation name')
    value=read(folder/'reservation.json')
    require(value['format']==FORMAT and value['store_id']==meta['store_id'] and
            value['name']==folder.name and value['directory_identity']==identity(private(folder,True)) and
            value['kind'] in RESERVATIONS and value['reserved_bytes']==RESERVATIONS[value['kind']],
            'generation ownership changed')
    return value


def inventory(root,meta):
    require(not (root/'gc.json').exists(),'unfinished prune; explicitly resume gc before admission')
    names=list((root/'generations').iterdir())
    require(len(names)<=256,'store generation limit')
    rows=[]
    for folder in sorted(names):
        value=reservation(folder,meta)
        active=busy(folder)
        row=dict(name=folder.name,kind=value['kind'],created_unix=value['created_unix'],
            identity=value['directory_identity'],state='ACTIVE' if active else 'UNSEALED',
            charged_bytes=value['reserved_bytes'],seal_sha256=None)
        if (folder/'seal.json').exists() and not active:
            seal=read(folder/'seal.json')
            require(seal['store_id']==meta['store_id'] and seal['name']==folder.name and
                    seal['tree']==tree(folder,value['reserved_bytes']),'sealed generation changed')
            row.update(state='SEALED',sealed_unix=seal['sealed_unix'],
                charged_bytes=seal['tree']['bytes']+(folder/'seal.json').stat().st_size,
                seal_sha256=pkg.sha(folder/'seal.json'))
        rows.append(row)
    return rows


def reserve(root,kind,binding,owner):
    require(kind in RESERVATIONS,'unknown service kind')
    with locked(root) as (root,meta):
        rows=inventory(root,meta);policy=meta['policy']
        require(len(rows)<policy['max_generations'] and
                sum(r['charged_bytes'] for r in rows)+RESERVATIONS[kind]<=policy['max_bytes'],
                'retention capacity exhausted; explicit prune or resolve unsealed generations')
        folder=root/'generations'/('g-'+secrets.token_hex(16));folder.mkdir(mode=0o700)
        fd=os.open(folder/'.lease',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            save(folder/'reservation.json',dict(format=FORMAT,store_id=meta['store_id'],name=folder.name,
                directory_identity=identity(folder.stat()),kind=kind,reserved_bytes=RESERVATIONS[kind],
                owner=owner,binding=binding,created_unix=time.time()))
        except BaseException:os.close(fd);raise
        return folder,fd


def seal(folder,proof,claim_fd):
    folder=Path(folder);root=folder.parent.parent
    with locked(root) as (root,meta):
        value=reservation(folder,meta)
        require(identity(os.fstat(claim_fd))==identity((folder/'.lease').stat()) and
                busy(folder) and not (folder/'seal.json').exists(),'seal requires live generation claim')
        require(proof.get('cleanup_confirmed') is True,'cleanup proof required')
        snapshot=tree(folder,value['reserved_bytes']-META_LIMIT)
        save(folder/'seal.json',dict(format=FORMAT,store_id=meta['store_id'],name=folder.name,
            sealed_unix=time.time(),proof=proof,tree=snapshot))


def plan(root,*,now=None):
    now=time.time() if now is None else now
    require(type(now) in (int,float) and 0<now<2**40,'invalid retention time')
    with locked(root) as (root,meta):
        rows=inventory(root,meta);policy=meta['policy']
        # Reserve room for the largest next generation. Active/unknown rows
        # are never eligible, even if a new start must be refused.
        charge=sum(r['charged_bytes'] for r in rows);count=len(rows);remove=[]
        for row in sorted(rows,key=lambda r:(r['created_unix'],r['name'])):
            if row['state']!='SEALED' or len(remove)>=2:continue
            if (now-row['sealed_unix']>policy['max_age_days']*86400 or
                count>=policy['max_generations'] or charge+max(RESERVATIONS.values())>policy['max_bytes']):
                remove.append(row);charge-=row['charged_bytes'];count-=1
        return dict(format=FORMAT,store_id=meta['store_id'],root_identity=meta['root_identity'],
            policy=policy,inventory=rows,remove=remove,retained_charged_bytes=charge,
            capacity_for_backend=count<policy['max_generations'] and
                charge+RESERVATIONS['backend']<=policy['max_bytes'],
            destructive=False,automatic=False)


def apply(root,expected):
    with locked(root) as (root,meta):
        require(expected['format']==FORMAT and expected['store_id']==meta['store_id'] and
                expected['root_identity']==meta['root_identity'] and expected['policy']==meta['policy'],
                'prune plan belongs to a different store')
        rows=inventory(root,meta)
        require(rows==expected['inventory'],'store changed since prune plan')
        current={r['name']:r for r in rows}
        require(len({r['name'] for r in expected['remove']})==len(expected['remove']) and
                all(current.get(r['name'])==r and r['state']=='SEALED' for r in expected['remove']),
                'prune targets must be unchanged sealed generations')
        require(shutil.rmtree.avoids_symlink_attacks,'descriptor-safe recursive removal unavailable')
        targets=[]
        for row in expected['remove']:
            folder=root/'generations'/row['name']
            with claim(folder):
                value=reservation(folder,meta);sealed=read(folder/'seal.json')
                require(pkg.sha(folder/'seal.json')==row['seal_sha256'] and
                        tree(folder,value['reserved_bytes'])==sealed['tree'],'prune target changed')
                snapshot=sealed['tree']
                for name in ('.lease','seal.json'):
                    info=private(folder/name)
                    snapshot['files'][name]=dict(identity=identity(info),bytes=info.st_size,
                        mtime_ns=info.st_mtime_ns,sha256=pkg.sha(folder/name))
                targets.append(dict(name=row['name'],trash='.trash-'+row['name'],tree=snapshot))
        require(len(targets)<=2,'prune batch limit')
        save(root/'gc.json',dict(format=FORMAT,store_id=meta['store_id'],root_identity=meta['root_identity'],
                                targets=targets,created_unix=time.time()))
        return _gc(root,meta)


def _remaining(folder,expected):
    """After an interrupted authorized deletion, only unchanged subsets remain."""
    require(identity(private(folder,True))==expected['directories']['.'],'prune root replaced')
    def visit(parent):
        for path in parent.iterdir():
            name=str(path.relative_to(folder));info=path.lstat()
            if stat.S_ISDIR(info.st_mode):
                require(name in expected['directories'] and identity(private(path,True))==
                        expected['directories'][name],'prune directory changed')
                visit(path)
            else:
                private(path);row=expected['files'].get(name)
                require(row is not None and identity(info)==row['identity'] and info.st_size==row['bytes'] and
                        pkg.sha(path)==row['sha256'],'prune file changed')
    visit(folder)


def _gc(root,meta):
    intent=read(root/'gc.json')
    require(intent['format']==FORMAT and intent['store_id']==meta['store_id'] and
            intent['root_identity']==meta['root_identity'] and len(intent['targets'])<=2,'unknown prune intent')
    for item in intent['targets']:
        require(NAME.fullmatch(item['name']) and item['trash']=='.trash-'+item['name'],'invalid prune target')
        old=root/'generations'/item['name'];trash=root/'generations'/item['trash']
        require(not (os.path.lexists(old) and os.path.lexists(trash)),'ambiguous prune rename')
        if os.path.lexists(old):
            require(not busy(old),'prune generation became active')
            _remaining(old,item['tree'])
        elif os.path.lexists(trash):_remaining(trash,item['tree'])
    removed=[]
    for item in intent['targets']:
        old=root/'generations'/item['name'];trash=root/'generations'/item['trash']
        if os.path.lexists(old):
            old.rename(trash)
            fd=os.open(root/'generations',os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        if os.path.lexists(trash):shutil.rmtree(trash)
        removed.append(item['name'])
    fd=os.open(root/'generations',os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    result=dict(applied=True,removed=removed,unknown_and_active_preserved=True,
                completed_unix=time.time(),intent_sha256=pkg.sha(root/'gc.json'))
    save(root/'last-gc.json',result)
    (root/'gc.json').unlink()
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return result


def resume_gc(root):
    with locked(root) as (root,meta):return _gc(root,meta)


def tail(root,generation,name,output,maximum=65536):
    require(NAME.fullmatch(generation) and type(maximum) is int and 1<=maximum<=META_LIMIT,'tail bounds')
    require(Path(name).as_posix()==name and not Path(name).is_absolute() and
            '..' not in Path(name).parts,'invalid log path')
    with locked(root) as (root,meta):
        rows={v['name']:v for v in inventory(root,meta)}
        require(generation in rows and rows[generation]['state']=='SEALED','only sealed private records are readable')
        folder=root/'generations'/generation;sealed=read(folder/'seal.json')
        require(name in sealed['tree']['files'],'unrecorded log file')
        with (folder/name).open('rb') as stream:
            stream.seek(max(0,sealed['tree']['files'][name]['bytes']-maximum));raw=stream.read(maximum)
        output=Path(output).absolute()
        require(output==output.resolve() and output.is_relative_to(ROOT/'.q4t-work') and
                not output.is_relative_to(root),'tail output must be new and outside store')
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        return dict(output=str(output),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),redacted=False)
