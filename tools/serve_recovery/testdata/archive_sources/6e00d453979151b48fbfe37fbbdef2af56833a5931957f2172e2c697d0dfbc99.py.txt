"""Explicit offline archive of retired default copies, with reversible rename.

This maintenance command never starts a model or deletes a package/evidence.
Current/history/pending generations stay in the bounded active pool. An exact
reviewed plan, STOPPED state and deployment/Guardian/GPU ownership are required.
"""
import argparse
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path

from tools.serve_host import native
from tools.serve_recovery import deployment, ledger, publication as publication
from tools.serve_recovery import publication_files as files, service
from tools.serve_service import package as engine

FORMAT='q4t-publication-archive-v1'
MAX_SELECTED=4


@contextmanager
def locked(state):
    state=files.directory(state)
    with ExitStack() as locks:
        locks.enter_context(deployment.claim(state,name='publication.lock'))
        desc=deployment.read_descriptor(state)
        manifest=publication.bundle.verify(desc['package'],desc['manifest_sha256'])
        lease=service.acquire_lease(manifest['lock_path']);locks.callback(os.close,lease)
        locks.enter_context(publication.quiescent(state,desc))
        deployment.ready(state,desc)
        current=publication.ready(state,desc)
        yield state,desc,manifest,current


def protected(current,intents):
    publication.policy.validate(current)
    keep={current['current']['generation_sha256']}
    for row in current['history']:
        keep.update(row[k]['generation_sha256'] for k in ('previous','selected'))
    if current['target']:keep.add(current['target']['generation_sha256'])
    # An immutable intent can precede the first journal write. Such a target
    # is still required to resume, even while the journal says stable.
    for intent in intents:
        if intent['base']['revision'] >= current['revision']:
            keep.update((intent['target']['generation_sha256'],
                         intent['base']['current']['generation_sha256']))
    return keep


def intents(state,desc):
    directory=state/publication.INTENTS
    names=sorted(directory.iterdir()) if directory.exists() else []
    ledger.require(len(names)<=publication.MAX_INTENTS,'publication intent bound')
    result=[]
    for path in names:
        ledger.require(path.suffix=='.json' and ledger.identifier(path.stem),
                       'partial or unknown publication intent requires resolution')
        result.append(publication.load_intent(state,desc,path.stem))
    return result


def references(value,prefix):
    if type(value) is str:return value==prefix or value.startswith(prefix+'/')
    if type(value) is dict:return any(references(v,prefix) for v in value.values())
    if type(value) is list:return any(references(v,prefix) for v in value)
    return False


def unreferenced(state,current,source):
    # Retired run logs and old immutable intents are historical evidence, not
    # live dependencies. Their original bytes and source packages stay intact.
    paths=[]
    for parent,dirs,names in os.walk(state,followlinks=False):
        dirs[:]=[n for n in dirs if n not in ('runs',publication.INTENTS)]
        for name in names:
            if name.endswith('.json'):paths.append(Path(parent)/name)
        ledger.require(len(paths)<=4096,'active state reference bound')
    root=Path(current['root'])
    for selected in [current['current'],*(r[k] for r in current['history']
                                         for k in ('previous','selected'))]:
        paths.append(files.generation(root,selected)/'service.release.json')
    total=0
    for path in set(paths):
        total+=path.stat().st_size
        ledger.require(total<=32*1024**2,'active state reference byte bound')
        value=native.private_json(path,4*1024**2)
        ledger.require(not references(value,str(source)),
                       'generation remains an active state/release dependency')


def verify_copy(path,row,root,state,desc):
    files.directory(path)
    ledger.require(native.inode(path)==row['identity'],'archive directory identity changed')
    value=native.private_json(path/'generation.json')
    ledger.require({p.name for p in path.iterdir()}==
        {*files.FILES,'service','intent.json','generation.json'} and
        native.private_json(path/'intent.json')==value and
        files.record(value)==row['selected'] and value['root']==str(root) and
        value['service_id']==desc['service_id'],'archive generation changed')
    for name in files.FILES:
        ledger.require((path/name).resolve()==path/name and
            engine.sha(path/name)==value['files'][name],'archive bytes changed')
    link=path/'service'
    ledger.require(link.is_symlink() and os.readlink(link)==row['selected']['package'],
                   'archive package link changed')
    files.accepted(path/'service.release.json',row['selected']['package'],
        row['selected']['manifest_sha256'],state,desc,
        decision_copy=path/'service.acceptance.json')


def prepare(state,output,generations):
    ledger.require(type(generations) is list and 1<=len(generations)<=MAX_SELECTED and
        len(set(generations))==len(generations) and
        all(type(v) is str and ledger.HASH.fullmatch(v) for v in generations),
        'one to four explicit generation hashes required')
    with locked(state) as (state,desc,manifest,current):
        root=Path(current['root']);output=Path(output).absolute()
        parent=files.directory(output.parent)
        repository=Path(manifest['repository'])
        ledger.require(output.is_relative_to(repository/'.q4t-work') and
            not output.is_relative_to(state) and not output.is_relative_to(root/files.DIRECTORY)
            and not os.path.lexists(output),'new private archive directory required')
        keep=protected(current,intents(state,desc))
        ledger.require(not keep.intersection(generations),'retained generation cannot be archived')
        rows=[]
        for digest in sorted(generations):
            path=root/files.DIRECTORY/'generations'/digest
            value=native.private_json(path/'generation.json');selected=files.record(value)
            ledger.require(selected['generation_sha256']==digest,'generation name differs')
            files.verify(root,state,desc,selected)
            unreferenced(state,current,path)
            ledger.require(path.stat().st_dev==parent.stat().st_dev,
                           'archive requires same filesystem atomic rename')
            rows.append(dict(selected=selected,identity=native.inode(path)))
        value=dict(format=FORMAT,state=str(state),service_id=desc['service_id'],root=str(root),
            archive=str(output),publication_revision=current['revision'],
            publication_sha256=ledger.digest(current),generations=rows,
            source_sha256=engine.sha(Path(__file__).resolve()))
        output.mkdir(mode=0o700);files.sync(parent)
        files.immutable_json(output/'plan.json',value)
        return dict(plan=str(output/'plan.json'),sha256=engine.sha(output/'plan.json'),
                    selected=generations,model_actions=False,files_moved=False)


def apply(path,expected,*,restore=False):
    path=Path(path).absolute()
    ledger.require(engine.sha(path)==expected,'reviewed archive plan changed')
    value=native.private_json(path)
    ledger.keys(value,('format','state','service_id','root','archive','publication_revision',
        'publication_sha256','generations','source_sha256'))
    archive=files.directory(value['archive'])
    ledger.require(value['format']==FORMAT and path==archive/'plan.json' and
        value['source_sha256']==engine.sha(Path(__file__).resolve()) and
        type(value['generations']) is list and 1<=len(value['generations'])<=MAX_SELECTED,
        'archive plan/source identity differs')
    with locked(value['state']) as (state,desc,manifest,current):
        root=Path(current['root']);pool=root/files.DIRECTORY/'generations'
        ledger.require(value['service_id']==desc['service_id'] and value['root']==str(root) and
            archive.is_relative_to(Path(manifest['repository'])/'.q4t-work') and
            not archive.is_relative_to(state) and not archive.is_relative_to(root/files.DIRECTORY),
            'archive service/root identity differs')
        if not restore:
            ledger.require(not (archive/'restored.json').exists() and
                value['publication_revision']==current['revision'] and
                value['publication_sha256']==ledger.digest(current),'publication/archive changed')
        else:
            receipt=native.private_json(archive/'retired.json')
            ledger.require(receipt.get('plan_sha256')==expected and receipt.get('status')=='RETIRED',
                           'completed archive required before restore')
        keep=protected(current,intents(state,desc));moves=[];seen=set()
        for row in value['generations']:
            ledger.keys(row,('selected','identity'));publication.policy.snapshot(row['selected'])
            digest=row['selected']['generation_sha256']
            ledger.require(digest not in seen,'duplicate archive generation');seen.add(digest)
            source,target=pool/digest,archive/digest
            if restore:source,target=target,source
            else:
                ledger.require(digest not in keep,'generation became retained')
                unreferenced(state,current,source)
            exists=[os.path.lexists(p) for p in (source,target)]
            ledger.require(sum(exists)==1,'archive source/destination collision or loss')
            verify_copy(source if exists[0] else target,row,root,state,desc)
            if exists[0]:moves.append((source,target))
        ledger.require(not restore or len(list(pool.iterdir()))+len(moves)<=files.MAX_GENERATIONS,
                       'restore exceeds active generation capacity')
        for source,target in moves:
            os.rename(source,target)
            files.sync(source.parent);files.sync(target.parent)
        # Re-establish rename durability even when a prior interrupted call
        # already moved every directory and this invocation has no moves.
        files.sync(pool);files.sync(archive)
        for row in value['generations']:
            verify_copy((pool if restore else archive)/row['selected']['generation_sha256'],
                        row,root,state,desc)
        ledger.require(publication.ready(state,desc)==current,'publication changed during archive')
        receipt=dict(status='RESTORED' if restore else 'RETIRED',plan_sha256=expected,
            publication_revision=current['revision'],generations=sorted(seen),
            current_unchanged=True,packages_unchanged=True,deleted_files=0,model_actions=False)
        files.immutable_json(archive/('restored.json' if restore else 'retired.json'),receipt)
        return receipt


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='mode',required=True)
    create=sub.add_parser('prepare');create.add_argument('--state',type=Path,required=True)
    create.add_argument('--output',type=Path,required=True)
    create.add_argument('--generation',action='append',required=True)
    for name in ('apply','restore'):
        command=sub.add_parser(name);command.add_argument('--plan',type=Path,required=True)
        command.add_argument('--sha256',required=True)
    args=parser.parse_args()
    value=prepare(args.state,args.output,args.generation) if args.mode=='prepare' else \
        apply(args.plan,args.sha256,restore=args.mode=='restore')
    print(ledger.canonical(value).decode())


if __name__=='__main__':main()
