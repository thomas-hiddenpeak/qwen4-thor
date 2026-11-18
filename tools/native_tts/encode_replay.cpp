// Real accepted speech through the production encoder and isolated decoder.
// CPU only; the outer driver owns any subsequent ASR GPU lease.
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>

#include "q4t/server/file_audio.h"
#include "q4t/server/output_schema.h"
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
void Run(const fs::path& plan_path, const fs::path& output) {
  io::Json plan;
  Require(io::ParseJson(Read(plan_path, 65536), &plan, false,
                        {.reject_duplicate_keys = true})
              .ok(),
          "invalid replay plan");
  Require(plan.GetString("schema") == "q4t-speech-encoding-content-v1",
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
    auto reply =
        EncodeSpeech(samples, format, limits, access, &encoded, control);
    Require(reply.ok(), reply.code + ": " + reply.message);
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
    Save(directory / "pcm.f32",
         {reinterpret_cast<const char*>(decoded.samples.data()),
          decoded.samples.size() * 4});
    Save(directory / "decoding.json", OutputJson(decoded.conversion));
    Save(directory / "result.json",
         "{\"samples\":" + std::to_string(decoded.samples.size()) +
             ",\"sample_rate\":16000,\"worker_starts\":2,\"worker_reaped\":2}"
             "\n");
  }
  Save(output / "result.json",
       "{\"status\":\"PASS_ENCODING_PENDING_CONTENT\",\"cases\":8,"
       "\"gpu_model_loads\":0,\"worker_starts\":16,\"worker_reaped\":16}\n");
}
}  // namespace
int SpeechEncodeReplay(int argc, char** argv) {
  if (argc < 2 || std::string_view(argv[1]) != "--speech-encode-replay")
    return -1;
  try {
    Require(argc == 4, "speech encoding replay PLAN OUTPUT");
    Run(argv[2], argv[3]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
