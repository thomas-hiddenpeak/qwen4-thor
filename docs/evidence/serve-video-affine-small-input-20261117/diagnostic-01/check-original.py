from pathlib import Path
import base64,hashlib,json,struct,subprocess,time
from PIL import Image
w=Path(__file__).resolve().parent
started=time.monotonic()
png=w/'frame-0.png'
run=subprocess.run([str(w/'process'),str(png),str(w/'pixels.f32')],capture_output=True,text=True,check=True)
shape=json.loads(run.stdout)
assert shape==dict(grid=[2,2,4],values=24576),shape
im=Image.open(png).convert('RGB');assert im.size==(64,32)
expected=[]
f32=lambda x:struct.unpack('<f',struct.pack('<f',x))[0]
for gt in range(2):
 for hb in range(1):
  for wb in range(2):
   for mb in range(2):
    for mj in range(2):
     for c in range(3):
      for t in range(2):
       for y in range(16):
        for x in range(16):
         byte=im.getpixel(((wb*2+mj)*16+x,(hb*2+mb)*16+y))[c]
         expected.append(f32((f32(byte/255.0)-0.5)/0.5))
actual=(w/'pixels.f32').read_bytes();reference=struct.pack('<'+str(len(expected))+'f',*expected)
assert actual==reference
samples={str(p):im.getpixel(p) for p in ((16,8),(48,8),(16,24),(48,24))}
assert list(samples.values())==[(255,0,0),(0,255,0),(0,0,255),(255,255,0)]
result=dict(status='PASS_CAPTURED_VIDEO_INPUT_CPU',seconds=time.monotonic()-started,grid=shape['grid'],pixel_values=shape['values'],bitwise_equal=True,visual_tokens_total=4,visual_tokens_per_group=2,source_png_sha256=hashlib.sha256(png.read_bytes()).hexdigest(),tensor_sha256=hashlib.sha256(actual).hexdigest(),corners=samples,source_files={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ['src/vision/processor.cpp','src/vision/media_plan.cpp']},gpu_actions=0)
(w/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
