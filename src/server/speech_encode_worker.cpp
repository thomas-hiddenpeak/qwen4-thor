#include "q4t/server/speech_encode.h"

#include <sys/resource.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cstring>
#include <mutex>

#include "file_video_protocol.h"
#include "media_codec_runtime.h"
#include "object_protocol.h"
#include "q4t/server/lifetime.h"
#include "speech_encode_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace speech_encode_detail;
#ifdef Q4T_HAVE_GSTREAMER_HEADERS
using media_detail::Runtime;
struct Input {
  Runtime& api;
  const std::string& source;
  std::mutex mutex;
  size_t offset = 0, total_bytes = 0;
  bool ended = false;
  std::atomic<Failure> failure{Failure::kNone};
  Input(Runtime& runtime, const std::string& pcm, size_t padding)
      : api(runtime), source(pcm), total_bytes(pcm.size() + padding * 2) {}
  static void Need(GstAppSrc* src, guint, gpointer opaque) noexcept {
    auto& self = *static_cast<Input*>(opaque);
    try {
      std::lock_guard lock(self.mutex);
      if (self.ended || self.failure != Failure::kNone) return;
      const size_t count =
          std::min<size_t>(4096, self.total_bytes - self.offset);
      if (!count) {
        self.ended = true;
        if (self.api.gst_app_src_end_of_stream_fn(src) != GST_FLOW_OK)
          self.failure = Failure::kInvalid;
        return;
      }
      auto* buffer =
          self.api.gst_buffer_new_allocate_fn(nullptr, count, nullptr);
      if (!buffer) {
        self.failure = Failure::kCapacity;
        return;
      }
      std::array<char, 4096> tail{};
      const char* data = tail.data();
      if (self.offset + count <= self.source.size()) {
        data = self.source.data() + self.offset;
      } else if (self.offset < self.source.size()) {
        std::memcpy(tail.data(), self.source.data() + self.offset,
                    self.source.size() - self.offset);
      }
      if (self.api.gst_buffer_fill_fn(buffer, 0, data, count) != count) {
        self.api.Mini(buffer);
        self.failure = Failure::kInvalid;
        return;
      }
      GST_BUFFER_OFFSET(buffer) = self.offset / 2;
      GST_BUFFER_OFFSET_END(buffer) = (self.offset + count) / 2;
      GST_BUFFER_PTS(buffer) = uint64_t(self.offset / 2) * GST_SECOND / 24000;
      GST_BUFFER_DURATION(buffer) =
          uint64_t((self.offset + count) / 2) * GST_SECOND / 24000 -
          GST_BUFFER_PTS(buffer);
      self.offset += count;
      if (self.api.gst_app_src_push_buffer_fn(src, buffer) != GST_FLOW_OK)
        self.failure = Failure::kInvalid;
    } catch (...) {
      self.failure = Failure::kCapacity;
    }
  }
};

Failure Encode(Runtime& api, const Request& request, const std::string& source,
               EncodedSpeech* output) {
  std::vector<const char*> names{"appsrc"};
  const char* output_caps = nullptr;
  switch (request.format) {
    case SpeechFormat::kMp3:
      names.insert(names.end(), {"lamemp3enc", "capsfilter"});
      output_caps = "audio/mpeg,mpegversion=1,layer=3,rate=24000,channels=1";
      break;
    case SpeechFormat::kAac:
      names.insert(names.end(), {"voaacenc", "capsfilter"});
      output_caps =
          "audio/mpeg,mpegversion=4,stream-format=adts,rate=24000,channels=1";
      break;
    case SpeechFormat::kFlac:
      names.insert(names.end(), {"flacenc", "capsfilter"});
      output_caps = "audio/x-flac,rate=24000,channels=1";
      break;
    case SpeechFormat::kOpus:
      names.insert(names.end(), {"opusenc", "oggmux"});
      break;
    default:
      return Failure::kInvalid;
  }
  names.push_back("appsink");
  const auto padding = EncoderTailSamples(request.format, request.samples);
  // Must outlive all callbacks and pipeline teardown.
  Input input(api, source, padding);
  auto* pipeline = api.gst_pipeline_new_fn(nullptr);
  if (!pipeline) return Failure::kCapacity;
  ScopeExit destroy([&] {
    api.gst_element_set_state_fn(pipeline, GST_STATE_NULL);
    api.gst_object_unref_fn(pipeline);
  });
  std::vector<GstElement*> nodes;
  for (const char* name : names) {
    auto* node = api.gst_element_factory_make_fn(name, nullptr);
    if (!node) return Failure::kDependency;
    if (!api.gst_bin_add_fn(reinterpret_cast<GstBin*>(pipeline), node)) {
      api.gst_object_unref_fn(node);
      return Failure::kInvalid;
    }
    nodes.push_back(node);
  }
  auto* caps = api.gst_caps_from_string_fn(
      "audio/x-raw,format=S16LE,layout=interleaved,rate=24000,channels=1");
  if (!caps) return Failure::kCapacity;
  api.g_object_set_fn(
      nodes[0], "caps", caps, "format", GST_FORMAT_TIME, "stream-type",
      GST_APP_STREAM_TYPE_STREAM, "is-live", FALSE, "max-bytes", guint64{16384},
      "block", TRUE, "size", gint64(input.total_bytes), "duration",
      guint64((request.samples + padding) * GST_SECOND / 24000), nullptr);
  api.Mini(caps);
  GstAppSrcCallbacks callbacks{};
  callbacks.need_data = Input::Need;
  api.gst_app_src_set_callbacks_fn(reinterpret_cast<GstAppSrc*>(nodes[0]),
                                   &callbacks, &input, nullptr);
  if (request.format == SpeechFormat::kMp3)
    api.g_object_set_fn(nodes[1], "target", gint{1}, "bitrate", gint{128},
                        "cbr", TRUE, nullptr);
  if (request.format == SpeechFormat::kAac)
    api.g_object_set_fn(nodes[1], "bitrate", gint{128000}, nullptr);
  if (request.format == SpeechFormat::kOpus)
    api.g_object_set_fn(nodes[1], "audio-type", gint{2048}, "bitrate",
                        gint{64000}, nullptr);
  if (request.format == SpeechFormat::kFlac)
    api.g_object_set_fn(nodes[1], "quality", gint{5}, nullptr);
  if (output_caps) {
    caps = api.gst_caps_from_string_fn(output_caps);
    if (!caps) return Failure::kCapacity;
    api.g_object_set_fn(nodes[2], "caps", caps, nullptr);
    api.Mini(caps);
  }
  api.g_object_set_fn(nodes.back(), "sync", FALSE, "max-buffers", guint{4},
                      "drop", FALSE, nullptr);
  for (size_t i = 0; i + 1 < nodes.size(); ++i)
    if (!api.gst_element_link_fn(nodes[i], nodes[i + 1]))
      return Failure::kInvalid;
  auto* bus = api.gst_element_get_bus_fn(pipeline);
  if (!bus) return Failure::kInvalid;
  ScopeExit release_bus([&] { api.gst_object_unref_fn(bus); });
  if (api.gst_element_set_state_fn(pipeline, GST_STATE_PLAYING) ==
      GST_STATE_CHANGE_FAILURE)
    return Failure::kInvalid;
  for (;;) {
    if (input.failure != Failure::kNone) return input.failure.load();
    auto* sample = api.gst_app_sink_try_pull_sample_fn(
        reinterpret_cast<GstAppSink*>(nodes.back()), 100000000);
    if (!sample) {
      auto* error =
          api.gst_bus_timed_pop_filtered_fn(bus, 0, GST_MESSAGE_ERROR);
      if (error) {
        api.Mini(error);
        return Failure::kInvalid;
      }
      if (api.gst_app_sink_is_eos_fn(
              reinterpret_cast<GstAppSink*>(nodes.back())))
        break;
      continue;  // Parent owns the wall deadline and can terminate this child.
    }
    ScopeExit release([&] { api.Mini(sample); });
    auto* buffer = api.gst_sample_get_buffer_fn(sample);
    if (!buffer) return Failure::kInvalid;
    const auto count = api.gst_buffer_get_size_fn(buffer);
    if (count > request.limits.max_output_bytes - output->bytes.size())
      return Failure::kCapacity;
    const auto offset = output->bytes.size();
    output->bytes.resize(offset + count);
    if (api.gst_buffer_extract_fn(buffer, 0, output->bytes.data() + offset,
                                  count) != count)
      return Failure::kInvalid;
  }
  api.gst_element_set_state_fn(pipeline, GST_STATE_NULL);
  if (input.failure != Failure::kNone) return input.failure.load();
  if (!input.ended || input.offset != input.total_bytes ||
      output->bytes.empty())
    return Failure::kInvalid;
  output->encoding =
      Object({{"format", String("q4t.speech_encode.v1")},
              {"codec", String(std::string(SpeechFormatName(request.format)))},
              {"samples", Number(request.samples)},
              {"sample_rate", Number(24000)},
              {"channels", Number(1)},
              {"source_pcm16_sha256", String(Digest(source))},
              {"output_sha256", String(Digest(output->bytes))},
              {"output_bytes", Number(output->bytes.size())},
              {"modules", api.modules}});
  if (request.format == SpeechFormat::kAac) {
    output->encoding.object.emplace_back("encoder_tail_samples",
                                         Number(padding));
    output->encoding.object.emplace_back("encoder_input_samples",
                                         Number(request.samples + padding));
  }
  return Failure::kNone;
}
#endif
int Run(int argc, char** argv, bool missing) {
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const rlimit cpu{30, 30};
  if (setrlimit(RLIMIT_CPU, &cpu)) return 2;
  Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.magic != kMagic || !ValidSpeechEncodeLimits(request.limits) ||
      !request.samples || request.samples > request.limits.max_samples ||
      SpeechFormatName(request.format).empty() ||
      request.format == SpeechFormat::kPcm ||
      request.format == SpeechFormat::kWav)
    return 2;
  std::string source(request.samples * 2, '\0');
  if (!detail::WorkerTransfer(source.data(), source.size(), false)) return 2;
  Ready ready = Ready::kDependency;
  Result result;
  EncodedSpeech encoded;
  int listener = -1;
#ifdef Q4T_HAVE_GSTREAMER_HEADERS
  Runtime api;
  if (!missing && api.Load(media_detail::CodecDomain::kSpeech)) {
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
    result.failure = Encode(api, request, source, &encoded);
  } catch (const std::bad_alloc&) {
    result.failure = Failure::kCapacity;
  } catch (...) {
    result.failure = Failure::kInvalid;
  }
#endif
  const auto metadata = result.failure == Failure::kNone
                            ? OutputJson(encoded.encoding)
                            : std::string{};
  if (result.failure == Failure::kNone) {
    result.metadata_bytes = metadata.size();
    result.bytes = encoded.bytes.size();
  }
  if (!detail::WorkerTransfer(&result, sizeof(result), true)) return 3;
  if (result.failure != Failure::kNone) return 0;
  return detail::WorkerTransfer(const_cast<char*>(metadata.data()),
                                metadata.size(), true) &&
                 detail::WorkerTransfer(encoded.bytes.data(),
                                        encoded.bytes.size(), true)
             ? 0
             : 3;
}
}  // namespace
int MaybeRunSpeechEncodeWorker(int argc, char** argv) {
  if (argc < 2) return -1;
  bool missing = false;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  missing = !std::strcmp(argv[1], "--speech-encode-missing-worker");
#endif
  if (std::strcmp(argv[1], "--speech-encode-worker") && !missing) return -1;
  try {
    return Run(argc, argv, missing);
  } catch (...) {
    return 3;
  }
}
}  // namespace q4t::server
