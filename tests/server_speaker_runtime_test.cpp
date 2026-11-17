#include "q4t/server/speaker_reference.h"

#include <algorithm>
#include <chrono>
#include <cstring>

#include "q4t/audio/speaker_config.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/server_options.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access() {
  return {"client:speaker-test", Clock::now() + 10s, {}};
}
std::string Wave(size_t samples) {
  std::string source(44 + samples * 2, '\0');
  std::memcpy(source.data(), "RIFF", 4);
  std::memcpy(source.data() + 8, "WAVEfmt ", 8);
  std::memcpy(source.data() + 36, "data", 4);
  const auto put = [&](size_t at, uint32_t value, size_t bytes) {
    for (size_t i = 0; i < bytes; ++i)
      source[at + i] = char((value >> (i * 8)) & 255);
  };
  put(4, source.size() - 8, 4);
  put(16, 16, 4);
  put(20, 1, 2);
  put(22, 1, 2);
  put(24, 16000, 4);
  put(28, 32000, 4);
  put(32, 2, 2);
  put(34, 16, 2);
  put(40, samples * 2, 4);
  for (size_t i = 0; i < samples; ++i) put(44 + i * 2, 8192, 2);
  return source;
}
std::string Data(std::string_view source) {
  constexpr std::string_view alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string output = "data:audio/wav;base64,";
  for (size_t i = 0; i < source.size(); i += 3) {
    const size_t n = std::min<size_t>(3, source.size() - i);
    uint32_t value = 0;
    for (size_t j = 0; j < n; ++j)
      value |= uint32_t(static_cast<unsigned char>(source[i + j]))
               << (16 - 8 * j);
    output += alphabet[(value >> 18) & 63];
    output += alphabet[(value >> 12) & 63];
    output += n > 1 ? alphabet[(value >> 6) & 63] : '=';
    output += n > 2 ? alphabet[value & 63] : '=';
  }
  return output;
}
bool Good(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
}  // namespace

Q4T_TEST(speaker_options_bind_required_components_and_atomic_budgets) {
  ServerOptions options;
  options.model_dir = "/readonly-main";
  const auto parse = [&](std::initializer_list<std::string_view> args) {
    return ParseServerOptions({args.begin(), args.size()}, &options).ok();
  };
  Q4T_CHECK(!parse({"--speaker-model-dir", "/optional/speaker"}));
  Q4T_CHECK(options.speaker_model_dir.empty());
  Q4T_CHECK(!parse({"--speaker-model-dir", "/optional/speaker",
                    "--asr-model-dir", "/optional/asr"}));
  Q4T_CHECK(parse({"--speaker-model-dir", "/optional/speaker",
                   "--asr-model-dir", "/optional/asr", "--aligner-model-dir",
                   "/optional/aligner", "--vad-model-dir", "/optional/vad"}));
  for (auto path : {"relative", "/", "/a/", "/a//b", "/a/../b"})
    Q4T_CHECK(!parse({"--speaker-model-dir", path}));
  for (auto flag :
       {"--speaker-window-ms", "--speaker-step-ms", "--speaker-max-windows",
        "--speaker-max-speakers", "--speaker-workspace-mib"})
    for (auto value : {"0", "-1", "2.5", "2147483647", "1x"})
      Q4T_CHECK(!parse({flag, value}));
  Q4T_CHECK(!parse({"--speaker-workspace-mib", "16"}));
  Q4T_CHECK(!parse({"--speaker-step-ms", "4000"}));
  Q4T_CHECK(options.speaker_limits.workspace_mib == 128);
  Q4T_CHECK(parse({"--speaker-window-ms", "30000", "--speaker-step-ms", "1000",
                   "--speaker-max-windows", "1024", "--speaker-max-speakers",
                   "64", "--speaker-workspace-mib", "512"}));
  Q4T_CHECK(options.speaker_limits.max_windows == 1024 &&
            options.speaker_limits.window_ms == 30000);
  return true;
}

Q4T_TEST(speaker_capabilities_report_native_identity_and_pending_overlap) {
  using C = q4t::audio::SpeakerConfig;
  for (bool enabled : {false, true}) {
    q4t::io::Json root;
    Q4T_CHECK(q4t::io::ParseJson(
        ChatCapabilitiesJson("chat", false, true, 0, nullptr, false, true, true,
                             true, 64, enabled),
        &root));
    const auto* asr = root.Find("transcriptions");
    const auto* speaker = asr ? asr->Find("speaker") : nullptr;
    Q4T_CHECK(speaker && speaker->GetBool("configured", !enabled) == enabled);
    Q4T_CHECK(speaker->GetString("profile") == C::kProfile &&
              speaker->GetString("revision") == C::kRevision &&
              speaker->GetString("weight_sha256") == C::kWeightSha256);
    Q4T_CHECK(!speaker->GetBool("core_readiness_dependency", true));
    Q4T_CHECK(speaker->GetString("reference_scope") == "current_request_only");
    Q4T_CHECK(asr->GetArray("remaining_native_work")->array[0].str ==
              "overlap_diarization");
    Q4T_CHECK(root.GetBool("implementation_only") &&
              !root.GetBool("release_attestation"));
    for (auto key : {"formats", "stream_formats"}) {
      const auto* values = asr->GetArray(key);
      Q4T_CHECK(values && std::any_of(values->array.begin(),
                                      values->array.end(), [](const auto& v) {
                                        return v.str == "diarized_json";
                                      }));
    }
  }
  return true;
}

Q4T_TEST(speaker_reference_inline_canonical_bytes_and_failed_owner) {
  const auto wave = Wave(32000);
  const auto data = Data(wave);
  FileInputContent content;
  Q4T_CHECK(ReadSpeakerReference(data, Access(), &content).ok());
  Q4T_CHECK(content.bytes == wave && content.mime_type == "audio/wav");
  const auto digest = content.sha256;
  for (const auto& value :
       {std::string("https://example.test/reference.wav"),
        std::string("file:///tmp/reference.wav"), std::string("/tmp/reference"),
        std::string("data:text/plain;base64,AAAA"),
        std::string("data:audio/wav;base64,AA=A"),
        std::string("data:audio/wav;base64,AB=="),
        data.substr(0, data.size() - 1), std::string((1ULL << 20) + 1, 'x')}) {
    Q4T_CHECK(!ReadSpeakerReference(value, Access(), &content).ok());
    Q4T_CHECK(content.bytes == wave && content.sha256 == digest);
  }
  return true;
}

Q4T_TEST(speaker_reference_actual_codec_enforces_two_to_ten_seconds) {
  for (size_t samples : {32000, 160000}) {
    FileAudioPcm pcm;
    Q4T_CHECK(
        Good(DecodeSpeakerReference(Data(Wave(samples)), Access(), &pcm)));
    Q4T_CHECK(pcm.samples.size() == samples);
    Q4T_CHECK(std::all_of(pcm.samples.begin(), pcm.samples.end(),
                          [](float x) { return x == .25f; }));
    Q4T_CHECK(pcm.duration_ns == samples * 62500);
  }
  for (size_t samples : {31999, 160001}) {
    FileAudioPcm pcm;
    pcm.samples = {42};
    Q4T_CHECK(
        !DecodeSpeakerReference(Data(Wave(samples)), Access(), &pcm).ok());
    Q4T_CHECK(pcm.samples == std::vector<float>{42});
  }
  detail::PollRequestWorkers();
  Q4T_CHECK(
      !detail::IsolatedCleanupPending(IsolatedWorkerDomain::kFileParse));
  return true;
}

Q4T_TEST(speaker_reference_scope_deadline_and_cancel_preserve_owner) {
  const auto data = Data(Wave(32000));
  for (int failure = 0; failure < 3; ++failure) {
    auto access = Access();
    if (failure == 0) access.scope.clear();
    if (failure == 1) access.deadline = Clock::now() - 1s;
    if (failure == 2) access.cancelled = [] { return true; };
    FileAudioPcm pcm;
    pcm.samples = {42};
    Q4T_CHECK(!DecodeSpeakerReference(data, access, &pcm).ok());
    Q4T_CHECK(pcm.samples == std::vector<float>{42});
  }
  detail::PollRequestWorkers();
  Q4T_CHECK(
      !detail::IsolatedCleanupPending(IsolatedWorkerDomain::kFileParse));
  return true;
}
