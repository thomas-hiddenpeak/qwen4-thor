"""CPU-only diagnostic ABI checks; fake CUDA, no driver/GPU execution."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.q4t-work/hc-up-persistent-control-20261017/cpu'
OUT.mkdir(parents=True, exist_ok=True)
SOURCE = ROOT / '.q4t-work/hc-up-address-observer-20261017/allocations.json'
records = json.loads(SOURCE.read_text())
sizes = {}
for variant in ('A', 'B'):
    sizes[variant] = [r['signature'][2] for r in records[variant]
                      if r['signature'][:2] == ['startup', 'cudaMalloc']]
assert sizes['A'] == sizes['B'] and len(sizes['A']) == 1541
manifest = OUT / 'sizes.txt'
manifest.write_text(''.join(f'{n}\n' for n in sizes['A']))
FAKE = r'''
#include <cuda.h>
#include <cuda_runtime_api.h>
#include <cassert>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
static unsigned allocations=0, imports=0, unmaps=0, releases=0, syncs=0;
extern "C" cudaError_t cudaMalloc(void** p,size_t) {
  assert(errno==EDOM);
  if (std::getenv("FAIL_ALLOC") && allocations==1534)
    return cudaErrorMemoryAllocation;
  *p=reinterpret_cast<void*>(uintptr_t{0x10000}+4096*allocations++);
  errno=ERANGE; return cudaSuccess;
}
extern "C" cudaError_t cudaFree(void* p) {
  assert(errno==EDOM);
  auto n=reinterpret_cast<uintptr_t>(p);
  assert(n>=0x10000 && n<0x10000+1541*4096 && n%4096==0);
  errno=ERANGE; return cudaSuccess;
}
extern "C" CUresult cuCtxGetDevice(CUdevice* d) {*d=0;return CUDA_SUCCESS;}
extern "C" CUresult cuMemGetAllocationGranularity(size_t* n,
    const CUmemAllocationProp*, CUmemAllocationGranularity_flags) {
  *n=2097152; return CUDA_SUCCESS;
}
extern "C" CUresult cuMemImportFromShareableHandle(
    CUmemGenericAllocationHandle* h,void* fd,CUmemAllocationHandleType type) {
  assert(type==CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR);
  assert(reinterpret_cast<intptr_t>(fd)==73+imports);
  *h=++imports; return CUDA_SUCCESS;
}
extern "C" CUresult cuMemAddressReserve(CUdeviceptr* p,size_t,size_t,
    CUdeviceptr hint,unsigned long long) {
  *p=hint+(std::getenv("BAD_HINT") ? 2097152 : 0); return CUDA_SUCCESS;
}
extern "C" CUresult cuMemMap(CUdeviceptr,size_t,size_t,
    CUmemGenericAllocationHandle,unsigned long long) {return CUDA_SUCCESS;}
extern "C" CUresult cuMemSetAccess(CUdeviceptr,size_t,
    const CUmemAccessDesc*,size_t) {return CUDA_SUCCESS;}
extern "C" CUresult cuCtxSynchronize() {++syncs;return CUDA_SUCCESS;}
extern "C" CUresult cuMemUnmap(CUdeviceptr,size_t) {++unmaps;return CUDA_SUCCESS;}
extern "C" CUresult cuMemAddressFree(CUdeviceptr,size_t) {return CUDA_SUCCESS;}
extern "C" CUresult cuMemRelease(CUmemGenericAllocationHandle) {
  ++releases;return CUDA_SUCCESS;
}
extern "C" void Verify() {
  assert(allocations==1541 && imports==3 && unmaps==3 && releases==3 && syncs==3);
}
'''
DRIVER = r'''
#include <cuda_runtime_api.h>
#include <cassert>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
extern "C" void Verify();
int main(int argc,char** argv) {
  assert(argc==2);
  FILE* f=std::fopen(argv[1],"r"); assert(f);
  void* pointers[1541];
  for (size_t i=0;i<1541;++i) {
    size_t n; assert(std::fscanf(f,"%zu",&n)==1);
    errno=EDOM;
    assert(cudaMalloc(&pointers[i],n)==cudaSuccess && errno==ERANGE);
    int role=i==1534 ? 0 : i==1535 ? 1 : i==1537 ? 2 : -1;
    uintptr_t expected=0x10000+4096*i;
    if (role>=0 && !std::strcmp(std::getenv("Q4T_DIAG_PERSISTENT_MODE"),"shared"))
      expected=0x4000000000ULL+role*0x100000000ULL+
          (!std::strcmp(std::getenv("Q4T_DIAG_PERSISTENT_SHIFT"),"512") ? 536870912 : 0);
    assert(reinterpret_cast<uintptr_t>(pointers[i])==expected);
  }
  assert(std::fclose(f)==0);
  for (int i=1540;i>=0;--i) {
    errno=EDOM;
    assert(cudaFree(pointers[i])==cudaSuccess && errno==ERANGE);
  }
  Verify();
}
'''
(OUT / 'fake.cpp').write_text(FAKE)
(OUT / 'driver.cpp').write_text(DRIVER)
flags = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-Werror', '-O2',
         '-I/usr/local/cuda-13.3/include']
for command in (
    flags + ['-shared', '-fPIC', str(OUT / 'fake.cpp'), '-o', str(OUT / 'fake.so')],
    flags + ['-shared', '-fPIC', str(ROOT / 'tools/evalscope/persistent_layout_probe.cpp'),
             '-ldl', '-o', str(OUT / 'probe.so')],
    flags + [str(OUT / 'driver.cpp'), str(OUT / 'fake.so'), '-o', str(OUT / 'driver')],
):
    subprocess.run(command, check=True)
results = []
def run(name, mode='shared', shift='0', changed=None, expected_error=None, **extra):
    env = os.environ.copy()
    env.update(LD_PRELOAD=str(OUT / 'probe.so'), Q4T_DIAG_PERSISTENT_MODE=mode,
               Q4T_DIAG_PERSISTENT_SHIFT=shift,
               Q4T_DIAG_PERSISTENT_MANIFEST=str(changed or manifest), **extra)
    # FDs belong only to this test process; no CUDA device files are opened.
    fds = []
    try:
        for target in (73, 74, 75):
            fd = os.open('/dev/null', os.O_RDONLY)
            if fd != target:
                os.dup2(fd, target)
                os.close(fd)
            fds.append(target)
        p = subprocess.run([str(OUT / 'driver'), str(manifest)], env=env,
                           pass_fds=fds, capture_output=True, text=True)
    finally:
        for fd in fds:
            os.close(fd)
    (OUT / f'{name}.log').write_text(p.stderr)
    if expected_error:
        assert p.returncode != 0 and expected_error in p.stderr, (name, p.stderr)
        assert 'cleanup_complete' not in p.stderr
    else:
        assert p.returncode == 0, (name, p.stderr)
        assert p.stderr.count('slot=') == 3
        assert 'startup_complete=1541' in p.stderr
        assert 'cleanup_complete=3' in p.stderr
    results.append({'name': name, 'exit': p.returncode, 'pass': True})
for mode in ('original', 'shared'):
    for shift in ('0', '512'):
        run(f'{mode}-{shift}', mode=mode, shift=shift)
for index in (0, 1536):
    copy = sizes['A'].copy()
    copy[index] += 1
    path = OUT / f'bad-{index}.txt'
    path.write_text(''.join(f'{n}\n' for n in copy))
    run(f'bad-sequence-{index}', changed=path,
        expected_error='startup allocation sequence mismatch')
path = OUT / 'truncated.txt'
path.write_text('32768\n')
run('truncated', changed=path, expected_error='invalid manifest entry')
run('allocation-failure', FAIL_ALLOC='1', expected_error='legacy allocation failed')
run('hint-failure', BAD_HINT='1', expected_error='exact VA unavailable')
report = {'cases': results, 'gpu_calls': 0,
          'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
          'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
          'startup_sequence_same_A_B': True, 'allocations': 1541,
          'scope': 'fake CUDA ABI, pointer/free translation and fail-closed gates; no model acceptance'}
(OUT / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
