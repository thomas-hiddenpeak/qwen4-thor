"""Generate an original moving robot reference clip and verify its decoded frames."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess

out = Path(__file__).resolve().parent
width, height, fps, frames = 128, 96, 6, 12
sources = []
for frame in range(frames):
    rgb = bytearray(bytes([25, 36, 55]) * (width * height))
    def rect(x, y, w, h, color):
        for row in range(y, y+h):
            off = (row * width + x) * 3
            rgb[off:off+w*3] = bytes(color) * w
    x = 12 + frame * 6
    rect(x+13, 14, 2, 8, (237, 190, 92))
    rect(x+11, 11, 6, 5, (242, 130, 64))
    rect(x+2, 22, 26, 20, (44, 208, 203))
    rect(x+7, 28, 4, 5, (18, 39, 63))
    rect(x+20, 28, 4, 5, (18, 39, 63))
    rect(x+5, 44, 21, 19, (44, 208, 203))
    rect(x, 46, 4, 12, (237, 190, 92))
    rect(x+27, 43-(frame%3)*2, 4, 12, (237, 190, 92))
    rect(x+6, 65, 5, 7+frame%2*3, (237, 190, 92))
    rect(x+20, 65, 5, 10-frame%2*3, (237, 190, 92))
    sources.append(bytes(rgb))
clip = out/'moving-robot.mp4'
gst=['gst-launch-1.0','-q']
assert not clip.exists()
encode=gst+['fdsrc','fd=0','!','rawvideoparse','format=rgb',f'width={width}',
            f'height={height}',f'framerate={fps}/1','!','videoconvert','!',
            'video/x-raw,format=I420','!','openh264enc','bitrate=500000','gop-size=6',
            'enable-frame-skip=false','!','h264parse','!','mp4mux','faststart=true',
            '!','filesink','location='+str(clip)]
subprocess.run(encode,input=b''.join(sources),check=True,timeout=30)
decode=gst+['filesrc','location='+str(clip),'!','qtdemux','!','h264parse','!',
            'openh264dec','!','videoconvert','!','video/x-raw,format=RGB',
            '!','fdsink','fd=1']
decoded=subprocess.check_output(decode,timeout=30)
size=width*height*3
assert len(decoded)==size*frames
centers=[]
for frame in range(frames):
    data=decoded[frame*size:(frame+1)*size]
    robot=[(i//3)%width for i in range(0,size,3)
           if data[i]<90 and data[i+1]>150 and data[i+2]>150]
    assert 700<len(robot)<1200
    centers.append(round(sum(robot)/len(robot),2))
assert all(5.5<b-a<6.5 for a,b in zip(centers,centers[1:]))
record=dict(source='Original geometric robot moving to the right, no human subject or external images.',
            program_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
            width=width,height=height,fps=fps,frames=frames,seconds=frames/fps,
            rgb_sha256=sha256(b''.join(sources)).hexdigest(),encoded_sha256=sha256(clip.read_bytes()).hexdigest(),
            encoded_bytes=clip.stat().st_size,decoded_centers=centers)
(out/'moving-robot.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record))
