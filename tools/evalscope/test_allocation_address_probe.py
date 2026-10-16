"""CPU-only ABI/recording check; fake CUDA library never loads a GPU driver."""
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.q4t-work/hc-up-address-observer-20261017/cpu'
OUT.mkdir(parents=True, exist_ok=True)
FAKE = r'''
#include <cuda_runtime_api.h>
#include <cassert>
#include <cerrno>
#include <cstdint>
static cudaError_t Alloc(void** p, size_t n) {
  assert(errno == EDOM);
  errno = ERANGE;
  if (n == 13) return cudaErrorMemoryAllocation;
  assert(n == 4096);
  *p = reinterpret_cast<void*>(0x12340000);
  return cudaSuccess;
}
extern "C" cudaError_t cudaMalloc(void** p, size_t n) {return Alloc(p,n);}
extern "C" cudaError_t cudaMallocHost(void** p, size_t n) {return Alloc(p,n);}
extern "C" cudaError_t cudaMallocAsync(void** p, size_t n, cudaStream_t s) {
  assert(s == reinterpret_cast<cudaStream_t>(0xabc)); return Alloc(p,n);
}
extern "C" cudaError_t cudaHostAlloc(void** p, size_t n, unsigned f) {
  assert(f == 3); return Alloc(p,n);
}
static cudaError_t Free(void* p) {
  assert(errno == EDOM); errno = ERANGE;
  if (!p) return cudaErrorInvalidValue;
  assert(p == reinterpret_cast<void*>(0x12340000)); return cudaSuccess;
}
extern "C" cudaError_t cudaFree(void* p) {return Free(p);}
extern "C" cudaError_t cudaFreeHost(void* p) {return Free(p);}
extern "C" cudaError_t cudaFreeAsync(void* p, cudaStream_t s) {
  assert(s == reinterpret_cast<cudaStream_t>(0xabc)); return Free(p);
}
'''
DRIVER = r'''
#include <cuda_runtime_api.h>
#include <cassert>
#include <cerrno>
#include <thread>
void Run() {
  auto s = reinterpret_cast<cudaStream_t>(0xabc);
  for (int i=0; i<20; ++i) {
    for (int api=0; api<4; ++api) {
      for (bool fail : {false,true}) {
        void* p = reinterpret_cast<void*>(0xdeadbeef);
        // An invalid output address must not be dereferenced on failure.
        void** slot = fail ? reinterpret_cast<void**>(1) : &p;
        size_t n = fail ? 13 : 4096;
        errno = EDOM;
        cudaError_t rc = api==0 ? cudaMalloc(slot,n) :
            api==1 ? cudaMallocAsync(slot,n,s) :
            api==2 ? cudaMallocHost(slot,n) : cudaHostAlloc(slot,n,3);
        assert(rc == (fail ? cudaErrorMemoryAllocation : cudaSuccess));
        assert(errno == ERANGE);
        if (!fail) assert(p == reinterpret_cast<void*>(0x12340000));
      }
    }
    for (int api=0; api<3; ++api) {
      for (bool fail : {false,true}) {
        void* p = fail ? nullptr : reinterpret_cast<void*>(0x12340000);
        errno = EDOM;
        cudaError_t rc = api==0 ? cudaFree(p) :
            api==1 ? cudaFreeAsync(p,s) : cudaFreeHost(p);
        assert(rc == (fail ? cudaErrorInvalidValue : cudaSuccess));
        assert(errno == ERANGE);
      }
    }
  }
}
int main() { std::thread a(Run),b(Run); a.join();b.join(); }
'''
(OUT / 'fake.cpp').write_text(FAKE)
(OUT / 'driver.cpp').write_text(DRIVER)
flags = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-Werror', '-O2',
         '-I/usr/local/cuda/include']
commands = [
    flags + ['-shared', '-fPIC', str(ROOT / 'tools/evalscope/allocation_address_probe.cpp'),
             '-ldl', '-o', str(OUT / 'probe.so')],
    flags + ['-shared', '-fPIC', str(OUT / 'fake.cpp'), '-o', str(OUT / 'fake.so')],
    flags + [str(OUT / 'driver.cpp'), str(OUT / 'fake.so'), '-pthread', '-o', str(OUT / 'driver')],
]
for cmd in commands:
    subprocess.run(cmd, check=True)
env = os.environ.copy()
env.pop('LD_PRELOAD', None)
subprocess.run([str(OUT / 'driver')], env=env, check=True)
env['LD_PRELOAD'] = str(OUT / 'probe.so')
p = subprocess.run([str(OUT / 'driver')], env=env, capture_output=True, text=True, check=True)
(OUT / 'records.log').write_text(p.stderr)
records = []
for line in p.stderr.splitlines():
    assert line.startswith('[allocation-address] '), line
    records.append(dict(re.findall(r'(\w+)=([^ ]+)', line)))
assert len(records) == 560
assert sorted(int(r['id']) for r in records) == list(range(560))
assert len({r['tid'] for r in records}) == 2
for r in records:
    assert int(r['end_ns']) >= int(r['begin_ns'])
    alloc = 'Alloc' in r['api'] or 'Malloc' in r['api']
    failed = r['rc'] != '0'
    assert r['ptr'] == ('(nil)' if failed else '0x12340000')
    assert int(r['bytes']) == ((13 if failed else 4096) if alloc else 0)
    assert r['stream'] == ('0xabc' if 'Async' in r['api'] else '(nil)')
    assert int(r['flags']) == (3 if r['api'] == 'cudaHostAlloc' else 0)
assert len({r['api'] for r in records}) == 7
summary = {'passed': True, 'records': len(records), 'threads': 2,
           'apis': sorted({r['api'] for r in records}),
           'checks': ['arguments', 'return codes', 'errno in/out',
                      'no invalid output-slot read on failure', 'pointer',
                      'concurrent unique IDs', 'complete parseable records'],
           'gpu_calls': 0, 'commands': commands}
(OUT / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary, indent=2))
