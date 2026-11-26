# Original CPU video fixtures

Six 96 × 64 solid RGB frames, at 2 fps: red, green, blue, yellow, cyan,
magenta. `generate.py --output .q4t-work/<new-directory>` creates the clips
using fixed software GStreamer OpenH264/VP8/VP9 encoders. No GPU is used.
These tiny checked-in files are test inputs, not generated build outputs.
The creation commands, original colors and SHA256 values are in identity.json.

OpenH264 prints `cmInitParaError` during encoder flushing on this host while
returning success. This original diagnostic is preserved. Actual native decode
checks all six source frames, four selected frames, color values, and PTS;
the encoder's exit status alone is not accepted as valid video evidence.

These are q4t-created synthetic assets with no third-party image content.
They exercise software decode and do not qualify Jetson hardware video decode.

`generate_nested.py` wraps the existing VP8 bytes in deterministic synthetic
Office alternate-content packages, including a two-level path and two videos.
These are parser boundary fixtures, not claims that a Word editor will render
an arbitrary video altChunk. `nested-identity.json` records exact input bytes.

`generate_business.py` creates the original 12-frame red-to-blue transition
clips at 2 fps for model business checks. The first six source frames are red
and the last six blue. `business-identity.json` records creation commands and
exact bytes; no color labels or expected answers are placed in the clip.

## 显示方向与非方形像素

`generate_geometry.py`生成原创128×64四象限RGB图案（左上红、右上绿、
左下蓝、右下黄），4帧/2fps。软件编码后，测试生成器仅修改MP4轨道
显示矩阵，覆盖90/180/270度及四种反射；`wide-pixels`/`tall-pixels`
覆盖2:1和1:2像素比例，`wide-rotate-90`覆盖比例与旋转组合。
实际媒体身份和生成命令见`geometry-identity.json`。没有GPU/模型访问，
未使用第三方图片或视频；生成器及产物均属项目测试资料。

`tall-pixels.mp4` 的 `tkhd` 声明 64×64，编码栅格为 128×64，像素比为 1:2。
按轨道宽高解码得到 64×64。旧版本曾输出 128×128；随附的
`tall-pixels-v3.json` 和 `tall-pixels-v3.png` 取自 rev550 的原生解码器，
其三帧 PNG 字节相同，用于验证历史快照仍可回读。

## MP4 alternate tracks

`alternate-tracks.mp4` contains the original three-second colour sequence and
the two-second rotated quadrants from this directory. The test toggles only
the enabled flags and compares decoded frames and timing to each original.
Reproduce it with FFmpeg stream copy from the fixture directory.

```sh
ffmpeg -nostdin -v error -i h264.mp4 -i rotate-90.mp4 -map 0:v:0 -map 1:v:0 \
  -c copy -disposition:v:0 default -disposition:v:1 0 -fflags +bitexact alternate-tracks.mp4
```

## QuickTime 矩形裁剪

`rect-clips.mov` 使用 `geometry.mp4` 的原始 H.264 码流。在轨道和影片各放入一个短 QuickDraw 矩形，测试可以独立改变两组坐标。运行 `python3 generate_rect_clips.py --output /tmp/rect-clips.mov` 可用 FFmpeg 流复制重建此夹具。
