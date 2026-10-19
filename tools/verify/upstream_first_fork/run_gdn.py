"""Bounded GDN-prefix replay and one explicitly recorded scalar intervention."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/upstream-gdn-20261020'
PRIOR = ROOT / '.q4t-work/upstream-first-fork-20261019/gpu-v1'
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    output = BASE/'gpu-v1'
    output.mkdir(exist_ok=False)
    state = dict(status='PREPARING', started=time.time())
    try:
        require(not any(k.startswith('Q4T_') for k in os.environ), 'unset Q4T overrides')
        fixture = output/'fixture'
        fixture.mkdir()
        old_files = json.loads((PRIOR/'files.json').read_text())
        provenance = {}
        def read(name):
            key = 'capture/upstream/' + name
            p = PRIOR/key
            require(sha(p) == old_files[key], 'prior capture hash')
            provenance[key] = old_files[key]
            return p.read_bytes()
        for target, source in [('qkv','gdn.qkv.y'), ('z','gdn.z.y'),
                               ('a','gdn.a.y'), ('beta','gdn.beta.y')]:
            batch = read('p72-t4-l0.'+source)
            single = b''.join(read(f'p{72+i}-t1-l0.'+source) for i in range(4))
            if target != 'a':
                require(batch == single, 'unexpected projected input fork')
            else:
                (fixture/'a_t1').write_bytes(single)
            (fixture/target).write_bytes(batch)
        for target, suffix in [('ssm.initial','ssm.before'), ('conv.initial','conv.before')]:
            data = read('p72-t4-l0.'+suffix)
            require(data == read('p72-t1-l0.'+suffix), 'initial state mismatch')
            (fixture/target).write_bytes(data)
        for i in range(4):
            for target, suffix in [('ssm','ssm.after'), ('conv','conv.after'), ('gated','gdn.out.x')]:
                (fixture/f'expected-t1-{i}.{target}').write_bytes(read(f'p{72+i}-t1-l0.'+suffix))
        for target,suffix in [('ssm','ssm.after'), ('conv','conv.after'), ('gated','gdn.out.x')]:
            (fixture/f'expected-t4.{target}').write_bytes(read('p72-t4-l0.'+suffix))
        # Read only four small BF16 tensors from the original content-identified shard.
        model_files = {r['path']:r for r in json.loads((PRIOR/'model-files.json').read_text())}
        for name in ['model.safetensors.index.json','config.json']:
            require(sha(MODEL/name) == model_files[name]['sha256'], 'model metadata changed')
        config = json.loads((MODEL/'config.json').read_text())['text_config']
        require(config['rms_norm_eps'] == 1e-6, 'norm epsilon')
        index = json.loads((MODEL/'model.safetensors.index.json').read_text())['weight_map']
        tensors = {}
        checked_shards = {}
        for target,suffix,count in [('conv1d','conv1d.weight',40960), ('norm','norm.weight',128),
                                     ('A_log','A_log',48), ('dt_bias','dt_bias',48)]:
            key = 'model.language_model.layers.0.linear_attn.'+suffix
            name = index[key]
            path = (MODEL/name).resolve()
            require(path.is_relative_to(MODEL), 'shard path')
            if name not in checked_shards:
                checked_shards[name] = sha(path)
                require(checked_shards[name] == model_files[name]['sha256'], 'model shard changed')
            with path.open('rb') as stream:
                size = struct.unpack('<Q',stream.read(8))[0]
                require(size < 16*1024*1024, 'safetensors header size')
                header = json.loads(stream.read(size))
                tensor = header[key]
                start,end = tensor['data_offsets']
                require(tensor['dtype']=='BF16' and end-start==count*2, 'tensor dtype/shape')
                stream.seek(8+size+start)
                data = stream.read(end-start)
                require(len(data)==count*2, 'tensor truncation')
            (fixture/target).write_bytes(data)
            tensors[target] = dict(key=key, shard=name, descriptor=tensor, sha256=sha(fixture/target))
        executable = output/'executable'
        shutil.copy2(ROOT/'.q4t-work/moe-replay-a1-20261019/build/q4t_upstream_gdn',executable)
        source_names = subprocess.check_output(['git','ls-files','src','include','CMakeLists.txt'],cwd=ROOT,text=True).splitlines()
        source_names += [str(p.relative_to(ROOT)) for p in Path(__file__).parent.glob('*') if p.is_file()]
        sources = {}
        for name in sorted(set(source_names)):
            path=output/'source'/name
            path.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(ROOT/name,path)
            sources[name]=sha(path)
        fixtures = {p.name:sha(p) for p in fixture.iterdir()}
        plan=dict(full_model_loads=0,pipeline_calls=8,checkpoint_calls=0,retries=0,
                  binary_sha256=sha(executable),sources=sources,fixture=fixtures,
                  original_capture=provenance,weight_tensors=tensors,shards=checked_shards,
                  intervention=dict(tensor='a',row=2,column=43,from_bf16=0x4020,to_bf16=0x4021),
                  cases=['t1-0','t1-1','t1-2','t1-3','t4','prefix1','prefix2','fixed-a'])
        (output/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
        command=[str(executable),str(fixture),str(output/'replay')]
        state.update(status='RUNNING',command=command)
        (output/'run.json').write_text(json.dumps(state,indent=2)+'\n')
        with (output/'gpu.log').open('w') as log:
            result=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        require(result.returncode==0,'GDN GPU failure')
        replay=output/'replay'
        require(json.loads((replay/'completion.json').read_text()) ==
                dict(status='PASS',pipeline_calls=8,checkpoint_calls=0),'completion')
        checks=[]
        def same(label,left,right):
            require(left==right,'bitexact mismatch: '+label)
            checks.append(dict(label=label,bytes=len(left),sha256=hashlib.sha256(left).hexdigest()))
        for case in ['t1-'+str(i) for i in range(4)]+['t4']:
            for kind in ['ssm','conv','gated']:
                same(case+'.'+kind,(replay/(case+'.'+kind)).read_bytes(),
                     (fixture/('expected-'+case+'.'+kind)).read_bytes())
        for count in [1,2]:
            for kind in ['ssm','conv']:
                same(f'prefix{count}.{kind}',(replay/f'prefix{count}.{kind}').read_bytes(),
                     (replay/f't1-{count-1}.{kind}').read_bytes())
            for kind in ['qkv_norm','raw_y','gated']:
                same(f'prefix{count}.{kind}',(replay/f'prefix{count}.{kind}').read_bytes(),
                     b''.join((replay/f't1-{i}.{kind}').read_bytes() for i in range(count)))
        for kind in ['ssm','conv']:
            same('fixed-a.'+kind,(replay/('fixed-a.'+kind)).read_bytes(),(replay/('t1-3.'+kind)).read_bytes())
        for kind in ['qkv_norm','raw_y','gated']:
            same('fixed-a.'+kind,(replay/('fixed-a.'+kind)).read_bytes(),
                 b''.join((replay/f't1-{i}.{kind}').read_bytes() for i in range(4)))
        for name,digest in fixtures.items(): require(sha(fixture/name)==digest,'fixture changed')
        for name,digest in sources.items():
            require(sha(ROOT/name)==digest and sha(output/'source'/name)==digest,'source changed')
        for name,digest in checked_shards.items(): require(sha(MODEL/name)==digest,'model shard changed')
        require(sha(executable)==plan['binary_sha256'],'binary changed')
        state.update(status='PASS',checks=checks)
    except BaseException as error:
        state.update(status='INTERRUPTED' if isinstance(error,KeyboardInterrupt) else 'FAIL',error=repr(error))
        raise
    finally:
        state['finished']=time.time()
        (output/'run.json').write_text(json.dumps(state,indent=2)+'\n')
        files={str(p.relative_to(output)):sha(p) for p in output.rglob('*') if p.is_file()}
        (output/'files.json').write_text(json.dumps(files,indent=2)+'\n')
    print('PASS GDN prefix states, original replay, single-a intervention',flush=True)


if __name__=='__main__':
    main()
