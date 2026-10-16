"""Fake CUPTI callbacks plus real SHA256; no CUDA driver/GPU is invoked."""
import hashlib,json,os,re,resource,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'.q4t-work/hc-up-module-observer-20261017/cpu'
OUT.mkdir(parents=True,exist_ok=True)
FAKE=r'''
#include <cupti.h>
#include <cassert>
#include <cstdlib>
static CUpti_CallbackFunc callback;
extern "C" CUptiResult CUPTIAPI cuptiSubscribe(CUpti_SubscriberHandle* s,
 CUpti_CallbackFunc f,void*) {
 if(std::getenv("FAIL_SUBSCRIBE")) return CUPTI_ERROR_NOT_SUPPORTED;
 callback=f;*s=reinterpret_cast<CUpti_SubscriberHandle>(1);return CUPTI_SUCCESS;
}
extern "C" CUptiResult CUPTIAPI cuptiEnableCallback(uint32_t enable,
 CUpti_SubscriberHandle,CUpti_CallbackDomain domain,CUpti_CallbackId id) {
 assert(enable==1 && domain==CUPTI_CB_DOMAIN_RESOURCE && id==CUPTI_CBID_RESOURCE_MODULE_LOADED);
 return CUPTI_SUCCESS;
}
extern "C" CUptiResult CUPTIAPI cuptiUnsubscribe(CUpti_SubscriberHandle) {
 return CUPTI_SUCCESS;
}
extern "C" void Emit(unsigned id) {
 CUpti_ModuleResourceData module{};
 module.moduleId=id;module.pCubin="abc";module.cubinSize=3;
 CUpti_ResourceData data{};data.context=reinterpret_cast<CUcontext>(0x1234);
 data.resourceDescriptor=std::getenv("BAD_DESCRIPTOR") ? nullptr : &module;
 callback(nullptr,CUPTI_CB_DOMAIN_RESOURCE,CUPTI_CBID_RESOURCE_MODULE_LOADED,&data);
}
'''
DRIVER=r'''
#include <thread>
extern "C" void Emit(unsigned);
int main() {
 std::thread a([]{for(unsigned i=0;i<8;++i)Emit(i);});
 std::thread b([]{for(unsigned i=8;i<16;++i)Emit(i);});
 a.join();b.join();
}
'''
(OUT/'fake.cpp').write_text(FAKE);(OUT/'driver.cpp').write_text(DRIVER)
flags=['g++-14','-std=c++23','-Wall','-Wextra','-Werror','-O2','-I/usr/local/cuda-13.3/targets/sbsa-linux/include']
for cmd in (
 flags+['-shared','-fPIC',str(OUT/'fake.cpp'),'-o',str(OUT/'fake.so')],
 flags+['-shared','-fPIC',str(ROOT/'tools/evalscope/module_load_probe.cpp'),'-l:libcrypto.so.3','-o',str(OUT/'probe.so')],
 flags+[str(OUT/'driver.cpp'),str(OUT/'fake.so'),'-pthread','-o',str(OUT/'driver')]):
 subprocess.run(cmd,check=True)
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
results=[]
for name,extra,error in [('valid',{},None),('descriptor',{'BAD_DESCRIPTOR':'1'},'missing descriptor'),
 ('subscribe',{'FAIL_SUBSCRIBE':'1'},'CUPTI operation failed'),
 ('enable',{'Q4T_DIAG_MODULE_OBSERVER':'0'},'explicit enable required')]:
 env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
 env.update(LD_PRELOAD=str(OUT/'probe.so'),Q4T_DIAG_MODULE_OBSERVER='1',**{})
 env.update(extra)
 p=subprocess.run([str(OUT/'driver')],env=env,capture_output=True,text=True)
 (OUT/f'{name}.log').write_text(p.stderr)
 if error:
  assert p.returncode!=0 and error in p.stderr and 'complete=' not in p.stderr,p.stderr
 else:
  assert p.returncode==0 and '[module-load] complete=16' in p.stderr,p.stderr
  rows=re.findall(r'sequence=(\d+) module_id=(\d+) context=(\S+) bytes=(\d+) sha256=(\w+)',p.stderr)
  assert len(rows)==16
  assert sorted(int(r[0]) for r in rows)==list(range(16))
  assert sorted(int(r[1]) for r in rows)==list(range(16))
  assert all(r[2]=='0x1234' and r[3]=='3' and r[4]==hashlib.sha256(b'abc').hexdigest() for r in rows)
 results.append({'case':name,'passed':True,'exit':p.returncode})
report={'gpu_calls':0,'cases':results,'scope':'fake callback ABI, real hash, concurrent IDs, fail-closed paths; no actual CUPTI module capture yet'}
(OUT/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
