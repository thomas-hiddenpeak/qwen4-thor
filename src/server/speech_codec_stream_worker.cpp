#include "q4t/server/speech_codec_stream.h"

#include <sys/resource.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <condition_variable>
#include <cstring>
#include <mutex>

#include "file_video_protocol.h"
#include "media_codec_runtime.h"
#include "object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/trace/sha256.h"
#include "speech_codec_stream_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace speech_stream_detail;

bool Send(EventKind kind, uint64_t bytes = 0,
          Failure failure = Failure::kNone) {
  Event event{kMagic, kind, bytes, failure};
  return detail::WorkerTransfer(&event, sizeof(event), true);
}

#ifdef Q4T_HAVE_GSTREAMER_HEADERS
using media_detail::Runtime;

// Exactly one input packet is retained. Only the main thread uses the worker
// socket; the appsrc callback waits here, never writes a protocol event.
struct PcmInput {
  Runtime& api;
  SpeechFormat format;
  std::mutex mutex;
  std::condition_variable changed;
  std::array<uint8_t, SpeechCodecStream::kInputSamples * 2> packet{};
  size_t bytes = 0, source_samples = 0, consumed_samples = 0;
  size_t encoder_samples = 0, tail_samples = 0;
  bool waiting = false, final = false, closed = false, ended = false;
  std::atomic<Failure> failure{Failure::kNone};

  PcmInput(Runtime& runtime, SpeechFormat f) : api(runtime), format(f) {}
  void Close() {
    std::lock_guard lock(mutex);
    closed = true;
    changed.notify_all();
  }
  bool WantsInput() {
    std::lock_guard lock(mutex);
    return waiting && !bytes && !final && !closed;
  }
  bool Supply(std::span<const uint8_t> data, bool eof) {
    std::lock_guard lock(mutex);
    if (!waiting || bytes || final || closed || data.size() > packet.size())
      return false;
    std::copy(data.begin(), data.end(), packet.begin());
    bytes = data.size();
    source_samples += bytes / 2;
    final = eof;
    if (final)
      tail_samples =
          speech_encode_detail::EncoderTailSamples(format, source_samples);
    waiting = false;
    changed.notify_one();
    return true;
  }
  bool Complete() {
    std::lock_guard lock(mutex);
    return ended && final && source_samples && !bytes && !tail_samples &&
           source_samples == consumed_samples &&
           encoder_samples ==
               source_samples + speech_encode_detail::EncoderTailSamples(
                                    format, source_samples);
  }
  static void Need(GstAppSrc* src, guint, gpointer opaque) noexcept {
    auto& self = *static_cast<PcmInput*>(opaque);
    try {
      std::array<uint8_t, SpeechCodecStream::kInputSamples * 2> data{};
      size_t count = 0, offset = 0;
      {
        std::unique_lock lock(self.mutex);
        if (self.closed || self.ended || self.failure != Failure::kNone) return;
        if (!self.bytes && !self.final) {
          self.waiting = true;
          self.changed.wait(
              lock, [&] { return self.bytes || self.final || self.closed; });
        }
        if (self.closed) return;
        offset = self.encoder_samples;
        if (self.bytes) {
          count = self.bytes;
          std::copy_n(self.packet.begin(), count, data.begin());
          self.bytes = 0;
          self.consumed_samples += count / 2;
        } else if (self.tail_samples) {
          const auto padding = std::min(self.tail_samples, data.size() / 2);
          count = padding * 2;
          self.tail_samples -= padding;
        } else {
          self.ended = true;
        }
        self.encoder_samples += count / 2;
      }
      // Never hold the input lock while pushing into GStreamer's bounded queue.
      if (!count) {
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
      if (self.api.gst_buffer_fill_fn(buffer, 0, data.data(), count) != count) {
        self.api.Mini(buffer);
        self.failure = Failure::kInvalid;
        return;
      }
      GST_BUFFER_OFFSET(buffer) = offset;
      GST_BUFFER_OFFSET_END(buffer) = offset + count / 2;
      GST_BUFFER_PTS(buffer) = uint64_t(offset) * GST_SECOND / 24000;
      GST_BUFFER_DURATION(buffer) =
          uint64_t(offset + count / 2) * GST_SECOND / 24000 -
          GST_BUFFER_PTS(buffer);
      if (self.api.gst_app_src_push_buffer_fn(src, buffer) != GST_FLOW_OK)
        self.failure = Failure::kInvalid;
    } catch (...) {
      self.failure = Failure::kCapacity;
    }
  }
};

Failure Encode(Runtime& api, const Request& request, io::Json* metadata) {
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
    case SpeechFormat::kOpus:
      names.insert(names.end(), {"opusenc", "oggmux"});
      break;
    case SpeechFormat::kFlac:
      names.insert(names.end(), {"flacenc", "capsfilter"});
      output_caps = "audio/x-flac,rate=24000,channels=1";
      break;
    default:
      return Failure::kInvalid;
  }
  names.push_back("appsink");
  PcmInput input(api, request.format);
  trace::Sha256Stream input_hash(request.limits.max_samples * 2);
  trace::Sha256Stream output_hash(request.limits.max_output_bytes);
  auto* pipeline = api.gst_pipeline_new_fn(nullptr);
  if (!pipeline) return Failure::kCapacity;
  ScopeExit destroy([&] {
    // Wake a callback waiting for more PCM before joining pipeline threads.
    input.Close();
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
  api.g_object_set_fn(nodes[0], "caps", caps, "format", GST_FORMAT_TIME,
                      "stream-type", GST_APP_STREAM_TYPE_STREAM, "is-live",
                      FALSE, "max-bytes", guint64{16384}, "block", TRUE, "size",
                      gint64{-1}, "duration", guint64{GST_CLOCK_TIME_NONE},
                      nullptr);
  api.Mini(caps);
  GstAppSrcCallbacks callbacks{};
  callbacks.need_data = PcmInput::Need;
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
  bool final = false;
  for (;;) {
    if (input.failure != Failure::kNone) return input.failure.load();
    auto* sample = api.gst_app_sink_try_pull_sample_fn(
        reinterpret_cast<GstAppSink*>(nodes.back()), 1000000);
    if (sample) {
      ScopeExit release([&] { api.Mini(sample); });
      auto* buffer = api.gst_sample_get_buffer_fn(sample);
      if (!buffer) return Failure::kInvalid;
      const auto count = api.gst_buffer_get_size_fn(buffer);
      if (count > request.limits.max_output_bytes - output_hash.bytes())
        return Failure::kCapacity;
      std::array<uint8_t, SpeechCodecStream::kOutputBytes> bytes{};
      for (size_t offset = 0; offset < count;) {
        const auto part = std::min(bytes.size(), count - offset);
        if (api.gst_buffer_extract_fn(buffer, offset, bytes.data(), part) !=
            part)
          return Failure::kInvalid;
        if (!output_hash.Add({bytes.data(), part})) return Failure::kCapacity;
        if (!Send(EventKind::kOutput, part) ||
            !detail::WorkerTransfer(bytes.data(), part, true))
          return Failure::kInvalid;
        offset += part;
      }
      continue;
    }
    auto* error = api.gst_bus_timed_pop_filtered_fn(bus, 0, GST_MESSAGE_ERROR);
    if (error) {
      api.Mini(error);
      return Failure::kInvalid;
    }
    if (api.gst_app_sink_is_eos_fn(reinterpret_cast<GstAppSink*>(nodes.back())))
      break;
    if (!final && input.WantsInput()) {
      if (!Send(EventKind::kNeedInput)) return Failure::kInvalid;
      Input next;
      if (!detail::WorkerTransfer(&next, sizeof(next), false) ||
          next.magic != kMagic ||
          next.samples > SpeechCodecStream::kInputSamples || next.final > 1 ||
          (!next.samples && !next.final))
        return Failure::kInvalid;
      if (next.samples > request.limits.max_samples - input_hash.bytes() / 2)
        return Failure::kCapacity;
      std::array<uint8_t, SpeechCodecStream::kInputSamples * 2> pcm{};
      const auto bytes = next.samples * 2;
      if (!detail::WorkerTransfer(pcm.data(), bytes, false))
        return Failure::kInvalid;
      if (!input_hash.Add({pcm.data(), bytes})) return Failure::kCapacity;
      final = next.final != 0;
      if (!input.Supply({pcm.data(), bytes}, final)) return Failure::kInvalid;
    }
  }
  input.Close();
  api.gst_element_set_state_fn(pipeline, GST_STATE_NULL);
  if (input.failure != Failure::kNone) return input.failure.load();
  if (!final || !input.Complete() || !output_hash.bytes())
    return Failure::kInvalid;
  const auto samples = input_hash.bytes() / 2;
  *metadata = Object(
      {{"format", String("q4t.speech_stream.v1")},
       {"codec", String(std::string(SpeechFormatName(request.format)))},
       {"samples", Number(samples)},
       {"sample_rate", Number(24000)},
       {"channels", Number(1)},
       {"source_pcm16_sha256", String(trace::HexDigest(input_hash.Snapshot()))},
       {"output_sha256", String(trace::HexDigest(output_hash.Snapshot()))},
       {"output_bytes", Number(output_hash.bytes())},
       {"modules", api.modules}});
  if (request.format == SpeechFormat::kAac) {
    const auto tail =
        speech_encode_detail::EncoderTailSamples(request.format, samples);
    metadata->object.emplace_back("encoder_tail_samples", Number(tail));
    metadata->object.emplace_back("encoder_input_samples",
                                  Number(samples + tail));
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
      SpeechFormatName(request.format).empty() ||
      request.format == SpeechFormat::kPcm ||
      request.format == SpeechFormat::kWav)
    return 2;
  Ready ready = Ready::kDependency;
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
  Failure failure = Failure::kDependency;
  io::Json metadata;
#ifdef Q4T_HAVE_GSTREAMER_HEADERS
  try {
    failure = Encode(api, request, &metadata);
  } catch (const std::bad_alloc&) {
    failure = Failure::kCapacity;
  } catch (...) {
    failure = Failure::kInvalid;
  }
#endif
  if (failure != Failure::kNone)
    return Send(EventKind::kFailure, 0, failure) ? 0 : 3;
  const auto text = OutputJson(metadata);
  return Send(EventKind::kDone, text.size()) &&
                 detail::WorkerTransfer(const_cast<char*>(text.data()),
                                        text.size(), true)
             ? 0
             : 3;
}
}  // namespace

int MaybeRunSpeechCodecStreamWorker(int argc, char** argv) {
  if (argc < 2) return -1;
  bool missing = false;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  missing = !std::strcmp(argv[1], "--speech-codec-stream-missing-worker");
#endif
  if (std::strcmp(argv[1], "--speech-codec-stream-worker") && !missing)
    return -1;
  try {
    return Run(argc, argv, missing);
  } catch (...) {
    return 3;
  }
}
}  // namespace q4t::server
