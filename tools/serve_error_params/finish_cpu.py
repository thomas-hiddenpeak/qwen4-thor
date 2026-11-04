"""Recheck generated source anchors, reusing unchanged native CPU evidence."""
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_error_params import cpu


def gate():
    original=cpu.read(cpu.BASE/'cpu-01/result.json')
    cpu.require(original['passed'] is False and
                {k for k,v in original['checks'].items() if not v}=={'generated'},'unexpected original failure')
    cpu.require(cpu.sources()==cpu.read(cpu.BASE/'build-01/sources.json'),'runtime/test source drift')
    built=cpu.read(cpu.BASE/'build-01/result.json')
    cpu.require(built['passed'] and all(cpu.sha(p)==v for p,v in built['binaries'].items()),'build identity drift')
    old=cpu.read(cpu.BASE/'generated-original/serve-input-semantics-v1.json')
    new=cpu.read(ROOT/'docs/serve-input-semantics-v1.json')
    # Source anchors are the only permitted document change. The semantic
    # descriptions and wire schemas remain exactly those tested above.
    def remove_hashes(value):
        if isinstance(value,dict):
            return {k:remove_hashes(v) for k,v in value.items()
                    if k not in ('sources_sha256','source_sha256')}
        if isinstance(value,list):return [remove_hashes(v) for v in value]
        return value
    cpu.require(remove_hashes(old)==remove_hashes(new),'generated semantics changed')
    return dict(original=cpu.sha(cpu.BASE/'cpu-01/result.json'),
                generated=cpu.sha(ROOT/'docs/serve-input-semantics-v1.json'))


def main():
    os.umask(0o077)
    out=cpu.BASE/'cpu-finish';out.mkdir()
    bindings=gate()
    passed=cpu.command(out,'generated',['python3','-B','tools/serve_protocol/build_contract.py','--check'])
    cpu.save(out/'result.json',dict(passed=passed,reused_native_and_differential=True,
        original_failed_gate='generated',bindings=bindings,source_sha256=cpu.sha(__file__),new_model=0,new_http=0))
    cpu.require(passed,'generated consistency failed')


if __name__=='__main__':main()
