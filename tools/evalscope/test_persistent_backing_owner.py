"""CPU-only owner lifecycle: fake driver exports memfds, no CUDA GPU calls."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.q4t-work/hc-up-persistent-owner-20261017/cpu'
OUT.mkdir(parents=True, exist_ok=True)
FAKE = r'''
#include <cuda.h>
#include <sys/mman.h>
#include <unistd.h>
#include <fcntl.h>
#include <cassert>
#include <cerrno>
#include <cstdint>
static int originals[3], exports[3], created=0, released=0;
extern "C" CUresult cuInit(unsigned) {return CUDA_SUCCESS;}
extern "C" CUresult cuDeviceGet(CUdevice* d,int n) {
  assert(n==0);*d=0;return CUDA_SUCCESS;
}
extern "C" CUresult cuDevicePrimaryCtxRetain(CUcontext* c,CUdevice) {
  *c=nullptr;return CUDA_SUCCESS;
}
extern "C" CUresult cuCtxSetCurrent(CUcontext) {return CUDA_SUCCESS;}
extern "C" CUresult cuMemGetAllocationGranularity(size_t* n,
    const CUmemAllocationProp*,CUmemAllocationGranularity_flags) {
  *n=2097152;return CUDA_SUCCESS;
}
extern "C" CUresult cuMemCreate(CUmemGenericAllocationHandle* h,size_t n,
    const CUmemAllocationProp* p,unsigned long long flags) {
  assert(created<3 && flags==0 && p->location.id==0);
  assert(n==(created<2 ? 167772160u : 2348810240u));
  int fd=memfd_create("fake-vmm",MFD_CLOEXEC);assert(fd>=0);
  originals[created]=fd;
  uint64_t value=created;assert(write(fd,&value,sizeof(value))==sizeof(value));
  *h=++created;return CUDA_SUCCESS;
}
extern "C" CUresult cuMemExportToShareableHandle(void* f,
    CUmemGenericAllocationHandle h,CUmemAllocationHandleType t,unsigned long long) {
  assert(t==CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR);
  *static_cast<int*>(f)=exports[h-1]=dup(originals[h-1]);return CUDA_SUCCESS;
}
extern "C" CUresult cuMemRelease(CUmemGenericAllocationHandle h) {
  assert(h==3-static_cast<unsigned>(released));
  errno=0;assert(fcntl(exports[h-1],F_GETFD)==-1 && errno==EBADF);
  assert(close(originals[h-1])==0);++released;return CUDA_SUCCESS;
}
extern "C" CUresult cuDevicePrimaryCtxRelease(CUdevice) {
  assert(created==3 && released==3);return CUDA_SUCCESS;
}
'''
(OUT / 'fake.cpp').write_text(FAKE)
flags = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-Werror', '-O2',
         '-I/usr/local/cuda-13.3/include']
subprocess.run(flags + [str(ROOT / 'tools/evalscope/persistent_backing_owner.cpp'),
                        str(OUT / 'fake.cpp'), '-o', str(OUT / 'owner')], check=True)
# Two fresh processes see the same open memfd backing, despite each closing its
# copies at exit. Descriptor offsets are deliberately irrelevant (pread/pwrite).
child = '''import os,struct,sys
step=int(sys.argv[1])
for i,fd in enumerate((73,74,75)):
 assert struct.unpack('Q',os.pread(fd,8,0))[0]==i+step*10
 os.pwrite(fd,struct.pack('Q',i+(step+1)*10),0)
'''
driver = '''import os,subprocess,sys
child=sys.argv[1]
ids=[(os.fstat(fd).st_dev,os.fstat(fd).st_ino) for fd in (73,74,75)]
assert len(set(ids))==3
for step in range(2):
 subprocess.run([sys.executable,'-c',child,str(step)],pass_fds=(73,74,75),check=True)
assert ids==[(os.fstat(fd).st_dev,os.fstat(fd).st_ino) for fd in (73,74,75)]
sys.exit(int(sys.argv[2]))
'''
results = []
for code in (0, 7):
    p = subprocess.run([str(OUT / 'owner'), sys.executable, '-c', driver, child,
                        str(code)], capture_output=True, text=True)
    (OUT / f'exit-{code}.log').write_text(p.stderr)
    assert p.returncode == code, p.stderr
    assert f'cleanup_complete=3 driver_exit={code}' in p.stderr
    results.append({'driver_exit': code, 'shared_memfd_two_children': True,
                    'owner_cleanup': True})
p = subprocess.run([str(OUT / 'owner'), '/no/such/stage-driver'],
                   capture_output=True, text=True)
assert p.returncode != 0 and 'POSIX operation failed' in p.stderr
assert 'cleanup_complete' not in p.stderr
(OUT / 'spawn-failure.log').write_text(p.stderr)
report = {'gpu_calls': 0, 'normal_and_failure_exit': results,
          'spawn_failure_rejected': True,
          'scope': 'CPU fake driver lifecycle, not real VMM or model evidence'}
(OUT / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
