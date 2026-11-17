from pathlib import Path
import json,runpy,sys
sys.dont_write_bytecode=True
root=Path(__file__).resolve().parents[2]
driver=runpy.run_path(str(root/'tools/serve_responses/video_affine_service_acceptance.py'))
package=Path(sys.argv[1]).resolve(strict=True)
driver['BASE']['LEGACY']['load_modules'](dict(package=dict(package=str(package))))
rows=driver['requests']()
assert rows['approved']['input'][0]['content'][0]['detail']=='high'
assert rows['approved']['store'] and rows['approved']['stream']
driver['check_sse']((root/'docs/evidence/serve-video-affine-business-20261117/shear-sse.wire').read_bytes())
assert driver['GLOBALS']['__file__'].endswith('video_affine_service_acceptance.py')
assert driver['GLOBALS']['ACCEPTANCE_PREFIX']=='VIDEO_AFFINE_SERVICE_'
prefixes=('tools.serve_recovery','tools.serve_service','tools.serve_ops','tools.serve_gateway','tools.serve_host')
verified=[]
for n,m in sys.modules.items():
 if any(n==p or n.startswith(p+'.') for p in prefixes) and getattr(m,'__file__',None):
  assert Path(m.__file__).resolve().is_relative_to(package/'source'),n
  verified.append(n)
print(json.dumps(dict(status='PASS_SEALED_DRIVER_IMPORTS',package=str(package),verified=len(verified),gpu_starts=0)))
