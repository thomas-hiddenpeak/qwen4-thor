// Real accepted speech through the production encoder and isolated decoder.
// CPU only; the outer driver owns any subsequent ASR GPU lease.
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>

#include "q4t/audio/pcm_output.h"
#include "q4t/server/file_audio.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/speech_codec_stream.h"
#include "q4t/server/speech_encode.h"
#include "q4t/trace/sha256.h"

namespace {
namespace fs = std::filesystem;
using namespace q4t;
using namespace q4t::server;
using Clock = std::chrono::steady_clock;
void Require(bool value, const std::string& message) {
  if (!value) throw std::runtime_error(message);
}
std::string Hash(std::string_view bytes) {
  return trace::HexDigest(trace::Sha256(
      {reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size()}));
}
std::string Read(const fs::path& path, size_t maximum) {
  Require(fs::is_regular_file(fs::symlink_status(path)),
          "regular fixture required");
  const auto size = fs::file_size(path);
  Require(size > 0 && size <= maximum, "fixture byte limit");
  std::string bytes(size, '\0');
  std::ifstream input(path, std::ios::binary);
  input.read(bytes.data(), bytes.size());
  Require(input.good(), "fixture read failed");
  return bytes;
}
void Save(const fs::path& path, std::string_view bytes) {
  std::ofstream output(path, std::ios::binary);
  output.write(bytes.data(), bytes.size());
  output.close();
  Require(output.good(), "output write failed");
}
ObjectReply Stream(std::span<const float> pcm, SpeechFormat format,
                   const SpeechEncodeLimits& limits, const BinaryAccess& access,
                   EncodedSpeech* encoded, GrammarControl control,
                   size_t* before_final) {
  SpeechCodecStream stream;
  auto reply = stream.Start(format, limits, access, control);
  if (!reply.ok()) return reply;
  size_t offset = 0;
  bool final = false;
  for (size_t step = 0; step < 10000; ++step) {
    SpeechCodecStep state;
    std::string_view bytes;
    reply = stream.Advance(&state, &bytes);
    if (!reply.ok()) return reply;
    if (state == SpeechCodecStep::kNeedInput) {
      const size_t count = std::min<size_t>(997, pcm.size() - offset);
      final = !count;
      reply = stream.Append(pcm.subspan(offset, count), final);
      if (!reply.ok()) return reply;
      offset += count;
    } else if (state == SpeechCodecStep::kOutput) {
      encoded->bytes += bytes;
      if (!final) *before_final += bytes.size();
    } else {
      encoded->encoding = stream.Metadata();
      return {};
    }
  }
  throw std::runtime_error("stream step budget exhausted");
}
void Run(const fs::path& plan_path, const fs::path& output, bool stream) {
  io::Json plan;
  Require(io::ParseJson(Read(plan_path, 65536), &plan, false,
                        {.reject_duplicate_keys = true})
              .ok(),
          "invalid replay plan");
  Require(
      plan.GetString("schema") == (stream ? "q4t-speech-stream-content-v1"
                                          : "q4t-speech-encoding-content-v1"),
      "plan schema");
  const auto* cases = plan.GetArray("cases");
  Require(cases && cases->array.size() == 8,
          "eight fixed codec cases required");
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh output required");
  constexpr SpeechFormat formats[] = {SpeechFormat::kMp3, SpeechFormat::kAac,
                                      SpeechFormat::kOpus, SpeechFormat::kFlac};
  for (size_t index = 0; index < 8; ++index) {
    const auto& item = cases->array[index];
    const auto format = formats[index % 4];
    Require(item.GetString("format") == SpeechFormatName(format) &&
                item.GetInt("utterance", -1) == int64_t(index / 4),
            "fixed case order differs");
    const auto raw = Read(item.GetString("pcm"), 76800 * 4);
    Require(raw.size() == (index < 4 ? 76800 : 74880) * 4 &&
                Hash(raw) == item.GetString("pcm_sha256"),
            "original speech identity differs");
    std::vector<float> samples(raw.size() / 4);
    std::memcpy(samples.data(), raw.data(), raw.size());
    const auto directory = output / ("case-" + std::to_string(index));
    Require(fs::create_directory(directory), "fresh case required");
    GrammarControl control;
    control.counters = std::make_shared<GrammarWorkerCounters>();
    control.compile_deadline = Clock::now() + std::chrono::seconds(5);
    BinaryAccess access{"test:speech-codec", control.compile_deadline, {}};
    EncodedSpeech encoded;
    SpeechEncodeLimits limits;
    limits.max_samples = samples.size();
    limits.max_output_bytes = 256 << 10;
    size_t before_final = 0;
    auto reply = stream ? Stream(samples, format, limits, access, &encoded,
                                 control, &before_final)
                        : EncodeSpeech(samples, format, limits, access,
                                       &encoded, control);
    Require(reply.ok(), reply.code + ": " + reply.message);
    if (stream)
      Require(before_final > 1024 && before_final < encoded.bytes.size(),
              "real progressive audio required");
    Save(directory / ("audio." + std::string(SpeechFormatName(format))),
         encoded.bytes);
    Save(directory / "encoding.json", OutputJson(encoded.encoding));
    FileAudioPcm decoded;
    FileAudioLimits decode_limits;
    decode_limits.max_output_samples = 60000;
    decode_limits.max_duration_ns = 3750000000ULL;
    reply = DecodeAudioContainer(encoded.bytes, decode_limits, access, &decoded,
                                 control);
    Require(reply.ok(), reply.code + ": " + reply.message);
    Require(decoded.samples.size() >= samples.size() * 2 / 3 &&
                decoded.samples.size() <= samples.size() * 2 / 3 + 2400,
            "codec priming/tail duration differs");
    Require(control.counters->started == 2 && control.counters->reaped == 2 &&
                control.counters->active == 0,
            "codec worker cleanup incomplete");
    const auto decoded_sha =
        Hash({reinterpret_cast<const char*>(decoded.samples.data()),
              decoded.samples.size() * 4});
    if (stream)
      Require(decoded_sha == item.GetString("accepted_decoded_sha256"),
              "stream decoded PCM differs from accepted actual content");
    Save(directory / "pcm.f32",
         {reinterpret_cast<const char*>(decoded.samples.data()),
          decoded.samples.size() * 4});
    Save(directory / "decoding.json", OutputJson(decoded.conversion));
    Save(directory / "result.json",
         "{\"samples\":" + std::to_string(decoded.samples.size()) +
             ",\"before_final_bytes\":" + std::to_string(before_final) +
             ",\"decoded_sha256\":\"" + decoded_sha +
             "\",\"sample_rate\":16000,\"worker_starts\":2,\"worker_reaped\":2}"
             "\n");
  }
  Save(output / "result.json",
       std::string("{\"status\":\"") +
           (stream ? "PASS_STREAM_DECODED_IDENTITY"
                   : "PASS_ENCODING_PENDING_CONTENT") +
           "\",\"cases\":8,\"gpu_model_loads\":0,\"worker_starts\":16,\"worker_"
           "reaped\":16}\n");
}
void DecodeHttp(const fs::path& plan_path, const fs::path& output) {
  io::Json plan;
  Require(io::ParseJson(Read(plan_path, 65536), &plan, false,
                        {.reject_duplicate_keys = true}).ok(),
          "invalid HTTP audio plan");
  Require(plan.GetString("schema") == "q4t-speech-http-decode-v1",
          "HTTP audio schema");
  const auto* cases = plan.GetArray("cases");
  Require(cases && !cases->array.empty() && cases->array.size() <= 8,
          "bounded HTTP audio cases required");
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh HTTP output required");
  for (size_t index = 0; index < cases->array.size(); ++index) {
    const auto& item = cases->array[index];
    auto bytes = Read(item.GetString("path"), 2 << 20);
    Require(Hash(bytes) == item.GetString("sha256"), "HTTP audio drift");
    if (item.GetString("format") == "pcm") {
      Require(bytes.size() % 2 == 0, "whole PCM16 samples required");
      std::string header;
      Require(audio::SpeechWaveHeader(bytes.size() / 2, (2 << 20) + 44,
                                      &header).ok(), "PCM WAV wrapper");
      bytes = header + bytes;  // Preserve every PCM16 byte; no requantizing.
    }
    const auto directory = output / ("case-" + std::to_string(index));
    Require(fs::create_directory(directory), "fresh HTTP case required");
    GrammarControl control;
    control.counters = std::make_shared<GrammarWorkerCounters>();
    control.compile_deadline = Clock::now() + std::chrono::seconds(5);
    BinaryAccess access{"test:speech-http", control.compile_deadline, {}};
    FileAudioLimits limits;
    limits.max_output_samples = 240000;
    limits.max_duration_ns = 15000000000ULL;
    FileAudioPcm decoded;
    const auto reply = DecodeAudioContainer(bytes, limits, access, &decoded,
                                            control);
    Require(reply.ok(), reply.code + ": " + reply.message);
    Require(!decoded.samples.empty() && decoded.samples.size() <= 240000,
            "decoded HTTP duration bound");
    Require(control.counters->started == 1 && control.counters->reaped == 1 &&
                control.counters->active == 0, "HTTP decoder cleanup");
    const std::string_view pcm{
        reinterpret_cast<const char*>(decoded.samples.data()),
        decoded.samples.size() * sizeof(float)};
    Save(directory / "pcm.f32", pcm);
    Save(directory / "decoding.json", OutputJson(decoded.conversion));
    Save(directory / "result.json",
         "{\"samples\":" + std::to_string(decoded.samples.size()) +
             ",\"decoded_sha256\":\"" + Hash(pcm) +
             "\",\"sample_rate\":16000,\"worker_starts\":1,"
             "\"worker_reaped\":1}\n");
  }
  Save(output / "result.json",
       "{\"status\":\"PASS_HTTP_DECODE_PENDING_CONTENT\",\"cases\":" +
           std::to_string(cases->array.size()) + ",\"gpu_model_loads\":0}\n");
}
}  // namespace
int SpeechEncodeReplay(int argc, char** argv) {
  if (argc < 2 || (std::string_view(argv[1]) != "--speech-encode-replay" &&
                   std::string_view(argv[1]) != "--speech-stream-replay" &&
                   std::string_view(argv[1]) != "--speech-http-decode"))
    return -1;
  try {
    Require(argc == 4, "speech encoding replay PLAN OUTPUT");
    if (std::string_view(argv[1]) == "--speech-http-decode")
      DecodeHttp(argv[2], argv[3]);
    else
      Run(argv[2], argv[3],
          std::string_view(argv[1]) == "--speech-stream-replay");
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
