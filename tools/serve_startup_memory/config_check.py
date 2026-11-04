"""Native startup CLI/config policy, including explicit bypass combinations."""
import json
import subprocess
from jsonschema import Draft202012Validator
from tools.serve_startup_memory.cpu import h, ROOT


def run(binary, output):
    output.mkdir()
    fixtures = [
        ('defaults', [], 2048, True),
        ('zero', ['--startup-memory-headroom-mib','0'], 0, True),
        ('explicit', ['--startup-memory-headroom-mib','4096'], 4096, True),
        ('maximum', ['--startup-memory-headroom-mib','1048576'], 1048576, True),
        ('no-budget', ['--no-budget'], 2048, False),
        ('no-budget-explicit', ['--no-budget','--startup-memory-headroom-mib','8192'], 8192, False),
        ('runtime-independent', ['--min-available-memory-mib','0'], 2048, True),
        ('negative', ['--startup-memory-headroom-mib','-1'], None, True),
        ('overflow', ['--startup-memory-headroom-mib','1048577'], None, True),
        ('noninteger', ['--startup-memory-headroom-mib','2GiB'], None, True)]
    h.save(output/'fixtures.json', fixtures)
    data=''.join(json.dumps(dict(argv=argv,effective_slots=1))+'\n' for _,argv,_,_ in fixtures)
    (output/'requests.jsonl').write_text(data)
    value=subprocess.run([str(binary)],input=data.encode(),capture_output=True,timeout=30)
    (output/'stdout.jsonl').write_bytes(value.stdout)
    (output/'stderr').write_bytes(value.stderr)
    h.require(value.returncode==0 and not value.stderr,'native probe execution')
    responses=[json.loads(line) for line in value.stdout.splitlines()]
    h.require(len(responses)==len(fixtures),'fixture count')
    schema=ROOT/'docs/serve-config-v1.schema.json'
    validator=Draft202012Validator(h.read(schema))
    failures=[]
    for (label,argv,headroom,budget),response in zip(fixtures,responses):
        try:
            h.require(response['accepted']==(headroom is not None),'CLI decision')
            if headroom is None:
                h.require(response['error']['error']['code']=='invalid_options','CLI error')
                continue
            config=response['configuration'];validator.validate(config)
            startup=config['startup_memory']
            h.require(startup['enabled']==bool(budget and headroom) and startup['checks'] is None,'offline claim')
            h.require(startup['headroom_mib']['requested']==headroom and
                startup['headroom_mib']['effective']==(headroom if budget else 0) and
                startup['headroom_mib']['source']==('cli' if '--startup-memory-headroom-mib' in argv else 'builtin_default'), 'source/value')
            h.require(startup['hard_limit'] is False and startup['snapshot_is_reservation'] is False,'hard-limit claim')
            h.require(config['memory_admission']['enabled']==(label!='runtime-independent'),'runtime independence')
        except Exception as error: failures.append(dict(label=label,error=repr(error)))
    h.save(output/'result.json',dict(passed=not failures,fixtures=len(fixtures),positives=7,
        failures=failures,binary=h.identity(binary),schema=h.identity(schema)))
    h.require(not failures,'startup config check failed')
