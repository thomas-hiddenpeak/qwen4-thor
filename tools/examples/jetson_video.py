"""Explicit local H.264 MP4 hardware decode using Jetson GStreamer elements.

Runs only in vision_input's bounded worker. Metadata, source, frame and
payload limits are checked before publication. Buffers retain native PTS.
There is no network source, automatic fallback, audio processing or model.
"""
import json
import math
from pathlib import Path
import time


def gst_modules():
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gst, GstVideo
    Gst.init(None)
    return Gst, GstVideo


def element(Gst, name):
    value = Gst.ElementFactory.make(name)
    if value is None:
        raise RuntimeError("Jetson codec dependency missing: " + name)
    return value


def decode(path, count, output):
    from PIL import Image
    from vision_input import MAX_PAYLOAD_BYTES
    # Use the explicitly software-only reader also used by the CPU adapter.
    # Jetson's minimal system ffprobe can reject valid locally encoded H.264.
    # Actual sampling/PTS below still comes only from the hardware pipeline.
    import cv2
    cv2.setNumThreads(1)
    metadata = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG,
        [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE])
    try:
        width, height, total, fps, code = [metadata.get(field) for field in
            (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT,
             cv2.CAP_PROP_FRAME_COUNT, cv2.CAP_PROP_FPS, cv2.CAP_PROP_FOURCC)]
        if not metadata.isOpened() or not all(math.isfinite(x) for x in
                (width,height,total,fps,code)) or fps <= 0:
            raise ValueError("cannot read local H.264 metadata")
        codec = int(code).to_bytes(4,"little").decode("ascii",errors="replace").lower()
        width,height,total = int(width),int(height),int(total)
        duration = total / fps
    finally:
        metadata.release()
    if (codec not in ("h264","avc1","avc3") or not count <= total <= 7200 or
            not math.isfinite(duration) or not 0 < duration <= 600 or
            not 1 <= width <= 4096 or not 1 <= height <= 4096 or
            width * height > 4096 * 2160):
        raise ValueError("Jetson H.264 metadata exceeds adapter limits")
    selected = {round(i * (total - 1) / (count - 1)) for i in range(count)}
    Gst, GstVideo = gst_modules()
    pipeline = Gst.Pipeline.new(None)
    names = ("filesrc", "qtdemux", "h264parse", "nvv4l2decoder",
             "nvvidconv", "capsfilter", "appsink")
    nodes = [element(Gst, name) for name in names]
    for node in nodes:
        pipeline.add(node)
    source, demux, parser, decoder, convert, caps, sink = nodes
    source.set_property("location", str(path))
    caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=RGBA"))
    sink.set_property("sync", False)
    sink.set_property("max-buffers", 1)
    sink.set_property("drop", False)
    if not source.link(demux):
        raise RuntimeError("source/demux link failed")
    for first, second in zip(nodes[2:], nodes[3:]):
        if not first.link(second):
            raise RuntimeError("Jetson decode link failed")
    link_errors = []
    def added(_demux, pad):
        if pad.get_current_caps().to_string().startswith("video/x-h264"):
            if pad.link(parser.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
                link_errors.append("H.264 demux link failed")
    demux.connect("pad-added", added)
    frames, files, timestamps, encoded = 0, [], [], 0
    bus = pipeline.get_bus()
    deadline = time.monotonic() + 55
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Jetson decoder failed to start")
        while time.monotonic() < deadline:
            sample = sink.emit("try-pull-sample", 100 * Gst.MSECOND)
            if sample is not None:
                if frames >= 7200:
                    raise ValueError("decoded frame limit exceeded")
                if frames in selected:
                    buffer = sample.get_buffer()
                    info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
                    timestamp = buffer.pts / Gst.SECOND
                    if (buffer.pts == Gst.CLOCK_TIME_NONE or timestamp < 0 or
                            timestamp > 86400 or info.width != width or
                            info.height != height or
                            (timestamps and timestamp <= timestamps[-1])):
                        raise ValueError("decoded geometry/PTS mismatch")
                    mapped, mapping = buffer.map(Gst.MapFlags.READ)
                    if not mapped:
                        raise RuntimeError("cannot map decoded frame")
                    try:
                        image = Image.frombytes("RGBA", (width, height),
                            mapping.data[info.offset[0]:], "raw", "RGBA",
                            info.stride[0]).convert("RGB")
                    finally:
                        buffer.unmap(mapping)
                    filename = f"frame-{len(files):02d}.png"
                    image.save(output / filename)
                    encoded += ((output / filename).stat().st_size + 2)//3*4+22
                    if encoded > MAX_PAYLOAD_BYTES:
                        raise ValueError("sampled video payload limit exceeded")
                    files.append(filename)
                    timestamps.append(timestamp)
                frames += 1
                continue
            message = bus.timed_pop_filtered(0, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if link_errors:
                raise RuntimeError(link_errors[0])
            if message:
                if message.type == Gst.MessageType.ERROR:
                    raise RuntimeError(str(message.parse_error()))
                break
        else:
            raise TimeoutError("Jetson decode worker deadline")
        if frames != total or len(files) != count:
            raise ValueError("container/decoded frame count mismatch")
        (output / "frames.json").write_text(json.dumps(dict(files=files,
            timestamps=timestamps, backend="jetson", elements=list(names),
            metadata_backend="opencv_ffmpeg_software_only", nominal_duration=duration)))
    finally:
        pipeline.set_state(Gst.State.NULL)
