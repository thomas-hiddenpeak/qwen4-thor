#include "q4t/server/file_audio.h"

#include <sys/resource.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <mutex>

#include "file_audio_protocol.h"
#include "file_video_protocol.h"
#include "media_codec_runtime.h"
#include "object_protocol.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace audio_detail;
#ifdef Q4T_HAVE_GSTREAMER_HEADERS
using media_detail::Runtime;
struct Pipeline {
  Runtime& api;
  const Request& request;
  const std::string& source;
  GstElement* pipeline = nullptr;
  GstElement* convert = nullptr;
  std::atomic<Failure> failure{Failure::kNone};
  std::mutex source_mutex;
  size_t offset = 0;
  std::mutex caps_mutex;
  GstAudioInfo raw_info{};
  uint64_t raw_frames = 0;
  std::atomic<bool> connected{false};
  std::atomic<uint64_t> target_frames{UINT64_MAX};
  bool end_seen = false;

  Pipeline(Runtime& runtime, const Request& options, const std::string& bytes)
      : api(runtime), request(options), source(bytes) {}

  static void Need(GstAppSrc* src, guint requested, gpointer opaque) noexcept {
    auto& self = *static_cast<Pipeline*>(opaque);
    try {
      std::lock_guard lock(self.source_mutex);
      const size_t size = std::min<size_t>(
          {self.source.size() - self.offset,
           requested == static_cast<guint>(-1) ? size_t{65536} : requested,
           size_t{65536}});
      if (!size) {
        self.api.gst_app_src_end_of_stream_fn(src);
        return;
      }
      auto* buffer =
          self.api.gst_buffer_new_allocate_fn(nullptr, size, nullptr);
      if (!buffer) {
        self.failure = Failure::kCapacity;
        return;
      }
      if (self.api.gst_buffer_fill_fn(
              buffer, 0, self.source.data() + self.offset, size) != size) {
        self.api.Mini(buffer);
        self.failure = Failure::kInvalid;
        return;
      }
      GST_BUFFER_OFFSET(buffer) = self.offset;
      self.offset += size;
      if (self.api.gst_app_src_push_buffer_fn(src, buffer) != GST_FLOW_OK)
        self.failure = Failure::kInvalid;
    } catch (...) {
      self.failure = Failure::kCapacity;
    }
  }
  static gboolean Seek(GstAppSrc*, guint64 offset, gpointer opaque) noexcept {
    auto& self = *static_cast<Pipeline*>(opaque);
    std::lock_guard lock(self.source_mutex);
    if (offset > self.source.size()) return FALSE;
    self.offset = offset;
    return TRUE;
  }
  static GstPadProbeReturn Raw(GstPad*, GstPadProbeInfo* info,
                               gpointer opaque) noexcept {
    auto& self = *static_cast<Pipeline*>(opaque);
    try {
      std::lock_guard lock(self.caps_mutex);
      if (GST_PAD_PROBE_INFO_TYPE(info) & GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM) {
        auto* event = GST_PAD_PROBE_INFO_EVENT(info);
        if (!event || GST_EVENT_TYPE(event) != GST_EVENT_CAPS)
          return GST_PAD_PROBE_OK;
        GstCaps* caps = nullptr;
        self.api.gst_event_parse_caps_fn(event, &caps);
        GstAudioInfo next{};
        if (!caps || !self.api.gst_audio_info_from_caps_fn(&next, caps) ||
            next.rate < 8000 || next.channels <= 0 || next.bpf <= 0) {
          self.failure = Failure::kInvalid;
          return GST_PAD_PROBE_DROP;
        }
        if (uint32_t(next.rate) > self.request.limits.max_sample_rate ||
            uint32_t(next.channels) > self.request.limits.max_channels) {
          self.failure = Failure::kCapacity;
          return GST_PAD_PROBE_DROP;
        }
        if (self.raw_info.rate && (next.rate != self.raw_info.rate ||
                                   next.channels != self.raw_info.channels ||
                                   next.bpf != self.raw_info.bpf)) {
          self.failure = Failure::kInvalid;
          return GST_PAD_PROBE_DROP;
        }
        self.raw_info = next;
      } else if (GST_PAD_PROBE_INFO_TYPE(info) & GST_PAD_PROBE_TYPE_BUFFER) {
        auto* buffer = GST_PAD_PROBE_INFO_BUFFER(info);
        const auto bytes = buffer ? self.api.gst_buffer_get_size_fn(buffer) : 0;
        if (!self.raw_info.bpf || bytes % self.raw_info.bpf) {
          self.failure = Failure::kInvalid;
          return GST_PAD_PROBE_DROP;
        }
        const auto frames = bytes / self.raw_info.bpf;
        const auto maximum =
            self.request.limits.max_decoded_values / self.raw_info.channels;
        if (frames > maximum - self.raw_frames) {
          self.failure = Failure::kCapacity;
          return GST_PAD_PROBE_DROP;
        }
        self.raw_frames += frames;
      }
      return GST_PAD_PROBE_OK;
    } catch (...) {
      self.failure = Failure::kCapacity;
      return GST_PAD_PROBE_DROP;
    }
  }
  bool Probe(GstPad* pad) {
    return pad && api.gst_pad_add_probe_fn(
                      pad,
                      GstPadProbeType(GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM |
                                      GST_PAD_PROBE_TYPE_BUFFER),
                      Raw, this, nullptr);
  }
  static GstPadProbeReturn End(GstPad* pad, GstPadProbeInfo* info,
                               gpointer opaque) noexcept {
    auto& self = *static_cast<Pipeline*>(opaque);
    auto* event = GST_PAD_PROBE_INFO_EVENT(info);
    if (!event || GST_EVENT_TYPE(event) != GST_EVENT_EOS)
      return GST_PAD_PROBE_OK;
    try {
      if (self.failure != Failure::kNone) return GST_PAD_PROBE_DROP;
      if (self.end_seen) return GST_PAD_PROBE_DROP;
      self.end_seen = true;
      std::lock_guard lock(self.caps_mutex);
      if (!self.raw_frames || self.raw_info.rate <= 0) {
        self.failure = Failure::kInvalid;
        return GST_PAD_PROBE_DROP;
      }
      const uint64_t wanted =
          (self.raw_frames * self.request.limits.output_sample_rate +
           self.raw_info.rate - 1) /
          self.raw_info.rate;
      const auto& limits = self.request.limits;
      if (wanted > limits.max_output_samples ||
          wanted > OutputSampleLimit(limits)) {
        self.failure = Failure::kCapacity;
        return GST_PAD_PROBE_DROP;
      }
      self.target_frames = wanted;
      if (uint32_t(self.raw_info.rate) == limits.output_sample_rate)
        return GST_PAD_PROBE_OK;
      // The stock EOS drain ends at the last source sample's timestamp,
      // which can omit a target sample within the source duration. Feed one
      // genuine zero-extension input frame through the SAME filter and keep
      // only ceil(source_frames * output_rate / source_rate) results. Never
      // fabricate an output sample or count the boundary guard as source.
      auto* caps = self.api.gst_pad_get_current_caps_fn(pad);
      GstAudioInfo converted{};
      const bool valid =
          caps && self.api.gst_audio_info_from_caps_fn(&converted, caps);
      if (caps) self.api.Mini(caps);
      if (!valid ||
          GST_AUDIO_INFO_FORMAT(&converted) != GST_AUDIO_FORMAT_F32LE ||
          converted.layout != GST_AUDIO_LAYOUT_INTERLEAVED ||
          converted.rate != self.raw_info.rate ||
          converted.channels != self.raw_info.channels) {
        self.failure = Failure::kInvalid;
        return GST_PAD_PROBE_DROP;
      }
      float zeros[8]{};
      auto* guard =
          self.api.gst_buffer_new_allocate_fn(nullptr, converted.bpf, nullptr);
      if (!guard) {
        self.failure = Failure::kCapacity;
        return GST_PAD_PROBE_DROP;
      }
      if (self.api.gst_buffer_fill_fn(guard, 0, zeros, converted.bpf) !=
          size_t(converted.bpf)) {
        self.api.Mini(guard);
        self.failure = Failure::kInvalid;
        return GST_PAD_PROBE_DROP;
      }
      GST_BUFFER_DURATION(guard) = GST_SECOND / converted.rate;
      // The source pad has already marked EOS before running this probe.
      // Its peer has not received EOS yet: feed the filter's sink directly,
      // then let this original event proceed and perform the normal drain.
      auto* sink = self.api.gst_pad_get_peer_fn(pad);
      if (!sink) {
        self.api.Mini(guard);
        self.failure = Failure::kInvalid;
        return GST_PAD_PROBE_DROP;
      }
      const auto flow = self.api.gst_pad_chain_fn(sink, guard);
      self.api.gst_object_unref_fn(sink);
      if (flow != GST_FLOW_OK) {
        self.failure = Failure::kInvalid;
        return GST_PAD_PROBE_DROP;
      }
      return GST_PAD_PROBE_OK;
    } catch (...) {
      self.failure = Failure::kCapacity;
      return GST_PAD_PROBE_DROP;
    }
  }
  static void Pad(GstElement*, GstPad* pad, gpointer opaque) noexcept {
    auto& self = *static_cast<Pipeline*>(opaque);
    try {
      self.Connect(pad);
    } catch (...) {
      self.failure = Failure::kCapacity;
    }
  }
  void Connect(GstPad* pad) {
    auto* caps = api.gst_pad_get_current_caps_fn(pad);
    if (!caps) {
      failure = Failure::kInvalid;
      return;
    }
    ScopeExit release([&] { api.Mini(caps); });
    const auto* shape = api.gst_caps_get_structure_fn(caps, 0);
    const char* mime = shape ? api.gst_structure_get_name_fn(shape) : nullptr;
    if (!mime || !std::string_view(mime).starts_with("audio/")) return;
    // A container's first audio track is the selected transcription source.
    // Other tracks are not decoded or silently mixed with it.
    if (connected.exchange(true)) return;
    const std::string_view type(mime);
    const char* decoder = nullptr;
    const char* parser = nullptr;
    if (type == "audio/x-opus")
      decoder = "opusdec";
    else if (type == "audio/x-vorbis")
      decoder = "vorbisdec";
    else if (type == "audio/x-flac")
      decoder = "flacdec";
    else if (type == "audio/x-alaw")
      decoder = "alawdec";
    else if (type == "audio/x-mulaw")
      decoder = "mulawdec";
    else if (type == "audio/mpeg") {
      int version = 0;
      if (!api.gst_structure_get_int_fn(shape, "mpegversion", &version)) {
        failure = Failure::kInvalid;
        return;
      }
      if (version == 4 || version == 2) {
        parser = "aacparse";
        decoder = "faad";
      } else if (version == 1) {
        parser = "mpegaudioparse";
        decoder = "mpg123audiodec";
      }
    }
    if (!decoder && type != "audio/x-raw") {
      failure = Failure::kDependency;
      return;
    }
    std::vector<GstElement*> added;
    for (const auto* name : {parser, decoder}) {
      if (!name) continue;
      auto* node = api.gst_element_factory_make_fn(name, nullptr);
      if (!node) {
        failure = Failure::kDependency;
        return;
      }
      if (!api.gst_bin_add_fn(reinterpret_cast<GstBin*>(pipeline), node)) {
        api.gst_object_unref_fn(node);
        failure = Failure::kInvalid;
        return;
      }
      added.push_back(node);
    }
    if (!added.empty()) {
      for (size_t i = 0; i + 1 < added.size(); ++i)
        if (!api.gst_element_link_fn(added[i], added[i + 1])) {
          failure = Failure::kInvalid;
          return;
        }
      if (!api.gst_element_link_fn(added.back(), convert)) {
        failure = Failure::kInvalid;
        return;
      }
    }
    auto* raw = added.empty()
                    ? pad
                    : api.gst_element_get_static_pad_fn(added.back(), "src");
    const bool probed = Probe(raw);
    if (!added.empty() && raw) api.gst_object_unref_fn(raw);
    auto* sink = api.gst_element_get_static_pad_fn(
        added.empty() ? convert : added.front(), "sink");
    const auto linked =
        sink ? api.gst_pad_link_fn(pad, sink) : GST_PAD_LINK_REFUSED;
    if (sink) api.gst_object_unref_fn(sink);
    if (!probed || linked != GST_PAD_LINK_OK) {
      failure = Failure::kInvalid;
      return;
    }
    for (auto* node : added)
      if (!api.gst_element_sync_state_with_parent_fn(node)) {
        failure = Failure::kInvalid;
        return;
      }
  }
};

Failure Decode(Runtime& api, const Request& request, const std::string& source,
               FileAudioPcm* output) {
  const auto kind = Detect(source);
  std::vector<const char*> names{"appsrc"};
  bool dynamic = false;
  switch (kind) {
    case Container::kWave: {
      names.push_back("wavparse");
      const auto tag = WaveTag(source);
      if (tag == 6)
        names.push_back("alawdec");
      else if (tag == 7)
        names.push_back("mulawdec");
      else if (tag == 2 || tag == 0x11)
        names.push_back("adpcmdec");
      else if (tag != 1 && tag != 3)
        return Failure::kDependency;
      break;
    }
    case Container::kFlac:
      names.insert(names.end(), {"flacparse", "flacdec"});
      break;
    case Container::kMpeg:
      names.insert(names.end(), {"mpegaudioparse", "mpg123audiodec"});
      break;
    case Container::kAdts:
      names.insert(names.end(), {"aacparse", "faad"});
      break;
    case Container::kOgg:
      names.push_back("oggdemux");
      dynamic = true;
      break;
    case Container::kMp4:
      names.push_back("qtdemux");
      dynamic = true;
      break;
    case Container::kWebm:
      names.push_back("matroskademux");
      dynamic = true;
      break;
    default:
      return Failure::kInvalid;
  }
  const auto convert_index = names.size();
  names.insert(names.end(),
               {"audioconvert", "audioresample", "capsfilter", "appsink"});
  auto* pipeline = api.gst_pipeline_new_fn(nullptr);
  if (!pipeline) return Failure::kCapacity;
  ScopeExit destroy([&] {
    api.gst_element_set_state_fn(pipeline, GST_STATE_NULL);
    api.gst_object_unref_fn(pipeline);
  });
  std::vector<GstElement*> nodes;
  for (const auto* name : names) {
    auto* node = api.gst_element_factory_make_fn(name, nullptr);
    if (!node) return Failure::kDependency;
    if (!api.gst_bin_add_fn(reinterpret_cast<GstBin*>(pipeline), node)) {
      api.gst_object_unref_fn(node);
      return Failure::kInvalid;
    }
    nodes.push_back(node);
  }
  Pipeline state{api, request, source};
  state.pipeline = pipeline;
  state.convert = nodes[convert_index];
  ScopeExit stop(
      [&] { api.gst_element_set_state_fn(pipeline, GST_STATE_NULL); });
  GstAppSrcCallbacks callbacks{};
  callbacks.need_data = Pipeline::Need;
  callbacks.seek_data = Pipeline::Seek;
  api.gst_app_src_set_callbacks_fn(reinterpret_cast<GstAppSrc*>(nodes[0]),
                                   &callbacks, &state, nullptr);
  api.g_object_set_fn(
      nodes[0], "stream-type", GST_APP_STREAM_TYPE_RANDOM_ACCESS, "format",
      GST_FORMAT_BYTES, "size", static_cast<gint64>(source.size()), "block",
      TRUE, "max-bytes", guint64{131072}, nullptr);
  api.g_object_set_fn(nodes.back(), "sync", FALSE, "max-buffers", guint{1},
                      "drop", FALSE, "wait-on-eos", FALSE, nullptr);
  api.g_object_set_fn(nodes[convert_index + 1], "quality", gint{10},
                      "resample-method", gint{4}, "sinc-filter-mode", gint{2},
                      "sinc-filter-auto-threshold", guint{1048576},
                      "sinc-filter-interpolation", gint{1}, nullptr);
  const auto output_caps = "audio/x-raw,format=F32LE,rate=" +
                           std::to_string(request.limits.output_sample_rate) +
                           ",layout=interleaved";
  auto* caps = api.gst_caps_from_string_fn(output_caps.c_str());
  if (!caps) return Failure::kCapacity;
  api.g_object_set_fn(nodes[convert_index + 2], "caps", caps, nullptr);
  api.Mini(caps);
  if (dynamic) {
    api.g_signal_connect_data_fn(nodes[1], "pad-added",
                                 reinterpret_cast<GCallback>(Pipeline::Pad),
                                 &state, nullptr, GConnectFlags(0));
  } else {
    auto* raw =
        api.gst_element_get_static_pad_fn(nodes[convert_index - 1], "src");
    const auto probed = state.Probe(raw);
    if (raw) api.gst_object_unref_fn(raw);
    if (!probed) return Failure::kInvalid;
    state.connected = true;
  }
  auto* converted =
      api.gst_element_get_static_pad_fn(nodes[convert_index], "src");
  const auto end_probe =
      converted ? api.gst_pad_add_probe_fn(converted,
                                           GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM,
                                           Pipeline::End, &state, nullptr)
                : 0;
  if (converted) api.gst_object_unref_fn(converted);
  if (!end_probe) return Failure::kInvalid;
  for (size_t i = 0; i + 1 < nodes.size(); ++i) {
    if (dynamic && i == 1) continue;
    if (!api.gst_element_link_fn(nodes[i], nodes[i + 1]))
      return Failure::kInvalid;
  }
  auto* bus = api.gst_element_get_bus_fn(pipeline);
  if (!bus) return Failure::kInvalid;
  ScopeExit release_bus([&] { api.gst_object_unref_fn(bus); });
  if (api.gst_element_set_state_fn(pipeline, GST_STATE_PLAYING) ==
      GST_STATE_CHANGE_FAILURE)
    return state.failure == Failure::kNone ? Failure::kInvalid
                                           : state.failure.load();
  std::vector<float> interleaved;
  for (;;) {
    if (state.failure != Failure::kNone) return state.failure.load();
    auto* sample = api.gst_app_sink_try_pull_sample_fn(
        reinterpret_cast<GstAppSink*>(nodes.back()), 100000000);
    if (!sample) {
      auto* message =
          api.gst_bus_timed_pop_filtered_fn(bus, 0, GST_MESSAGE_ERROR);
      if (message) {
        api.Mini(message);
        return state.failure == Failure::kNone ? Failure::kInvalid
                                               : state.failure.load();
      }
      if (api.gst_app_sink_is_eos_fn(
              reinterpret_cast<GstAppSink*>(nodes.back())))
        break;
      continue;
    }
    ScopeExit release([&] { api.Mini(sample); });
    GstAudioInfo info{};
    auto* caps = api.gst_sample_get_caps_fn(sample);
    auto* buffer = api.gst_sample_get_buffer_fn(sample);
    if (!caps || !buffer || !api.gst_audio_info_from_caps_fn(&info, caps) ||
        GST_AUDIO_INFO_FORMAT(&info) != GST_AUDIO_FORMAT_F32LE ||
        info.layout != GST_AUDIO_LAYOUT_INTERLEAVED ||
        uint32_t(info.rate) != request.limits.output_sample_rate ||
        info.channels <= 0 ||
        uint32_t(info.channels) > request.limits.max_channels)
      return Failure::kInvalid;
    const auto bytes = api.gst_buffer_get_size_fn(buffer);
    if (!bytes || bytes % info.bpf) return Failure::kInvalid;
    const auto target = state.target_frames.load();
    if (output->samples.size() > target) return Failure::kInvalid;
    const auto count =
        std::min<uint64_t>(bytes / info.bpf, target - output->samples.size());
    const auto maximum = std::min<uint64_t>(request.limits.max_output_samples,
                                            OutputSampleLimit(request.limits));
    if (count > maximum - output->samples.size()) return Failure::kCapacity;
    interleaved.resize(bytes / sizeof(float));
    if (api.gst_buffer_extract_fn(buffer, 0, interleaved.data(), bytes) !=
        bytes)
      return Failure::kInvalid;
    for (size_t i = 0; i < count; ++i) {
      double sum = 0;
      for (int c = 0; c < info.channels; ++c) {
        const float value = interleaved[i * info.channels + c];
        if (!std::isfinite(value)) return Failure::kInvalid;
        sum += value;
      }
      const float mean = sum / info.channels;
      if (!std::isfinite(mean)) return Failure::kInvalid;
      output->samples.push_back(mean);
    }
  }
  api.gst_element_set_state_fn(pipeline, GST_STATE_NULL);
  if (state.failure != Failure::kNone) return state.failure.load();
  if (output->samples.empty() || !state.connected || !state.raw_frames)
    return Failure::kInvalid;
  if (output->samples.size() != state.target_frames) return Failure::kInvalid;
  output->sample_rate = request.limits.output_sample_rate;
  output->duration_ns =
      OutputDuration(output->samples.size(), output->sample_rate);
  const std::string_view bytes(
      reinterpret_cast<const char*>(output->samples.data()),
      output->samples.size() * sizeof(float));
  output->conversion =
      Object({{"format", String("q4t.cpu_audio_f32le.v1")},
              {"source_sha256", String(Digest(source))},
              {"source_bytes", Number(source.size())},
              {"pcm_sha256", String(Digest(bytes))},
              {"samples", Number(output->samples.size())},
              {"sample_rate", Number(output->sample_rate)},
              {"source_sample_rate", Number(state.raw_info.rate)},
              {"source_channels", Number(state.raw_info.channels)},
              {"source_frames", Number(state.raw_frames)},
              {"track_policy", String("first-audio-track")},
              {"mono_policy", String("resample-channels-f64-mean")},
              {"resample_quality", Number(10)},
              {"resample_table", String("auto-1MiB-linear")},
              {"resample_boundary", String("zero-extend-to-source-ceil")},
              {"modules", api.modules}});
  return Failure::kNone;
}
#endif
int Run(int argc, char** argv, bool missing) {
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const rlimit cpu{30, 30};
  if (setrlimit(RLIMIT_CPU, &cpu)) return 2;
  Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.magic != kMagic || !ValidFileAudioLimits(request.limits) ||
      !request.bytes || request.bytes > request.limits.max_source_bytes)
    return 2;
  std::string source(request.bytes, '\0');
  if (!detail::WorkerTransfer(source.data(), source.size(), false) ||
      !IsAudioContainer(source))
    return 2;
  Ready ready = Ready::kDependency;
  Result result;
  FileAudioPcm pcm;
  int listener = -1;
#ifdef Q4T_HAVE_GSTREAMER_HEADERS
  Runtime api;
  if (!missing && api.Load(media_detail::CodecDomain::kAudio)) {
    listener = video_detail::EnterVideoSandbox();
    ready = listener >= 0 ? Ready::kReady : Ready::kSandbox;
  }
#else
  (void)missing;
#endif
  if (!detail::WorkerTransfer(&ready, sizeof(ready), true)) return 3;
  if (ready != Ready::kReady) return 0;
  if (!detail::SendWorkerIoListener(listener)) return 3;
  close(listener);
#ifdef Q4T_HAVE_GSTREAMER_HEADERS
  try {
    result.failure = Decode(api, request, source, &pcm);
  } catch (const std::bad_alloc&) {
    result.failure = Failure::kCapacity;
  } catch (...) {
    result.failure = Failure::kInvalid;
  }
#endif
  const auto metadata = result.failure == Failure::kNone
                            ? OutputJson(pcm.conversion)
                            : std::string{};
  if (result.failure == Failure::kNone) {
    result.metadata_bytes = metadata.size();
    result.samples = pcm.samples.size();
  }
  if (!detail::WorkerTransfer(&result, sizeof(result), true)) return 3;
  if (result.failure != Failure::kNone) return 0;
  return detail::WorkerTransfer(const_cast<char*>(metadata.data()),
                                metadata.size(), true) &&
                 detail::WorkerTransfer(pcm.samples.data(),
                                        pcm.samples.size() * sizeof(float),
                                        true)
             ? 0
             : 3;
}
}  // namespace
int MaybeRunFileAudioWorker(int argc, char** argv) {
  if (argc < 2) return -1;
  bool missing = false;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  missing = !std::strcmp(argv[1], "--file-audio-missing-worker");
#endif
  if (std::strcmp(argv[1], "--file-audio-worker") && !missing) return -1;
  try {
    return Run(argc, argv, missing);
  } catch (...) {
    return 3;
  }
}
}  // namespace q4t::server
