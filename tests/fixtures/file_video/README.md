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
