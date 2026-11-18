// Bounded real-speech time scaling. No model or GPU work in this executable.
#include <sys/stat.h>

#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

#include "q4t/audio/pcm_output.h"
#include "q4t/audio/speech_tempo.h"
#include "q4t/io/json.h"
#include "q4t/trace/sha256.h"

namespace {
namespace fs = std::filesystem;
using namespace q4t;
void Require(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}
void Check(const Status& status) {
  Require(status.ok(), status.message().c_str());
}
std::string Read(const fs::path& path, size_t limit) {
  Require(fs::is_regular_file(fs::symlink_status(path)),
          "regular file required");
  const auto size = fs::file_size(path);
  Require(size <= limit, "fixture byte budget exceeded");
  std::string result(size, '\0');
  std::ifstream input(path, std::ios::binary);
  input.read(result.data(), size);
  Require(input.good(), "fixture read failed");
  return result;
}
void Save(const fs::path& path, std::string_view bytes) {
  std::ofstream out(path, std::ios::binary);
  out.write(bytes.data(), bytes.size());
  out.close();
  Require(out.good(), "fixture write failed");
}
void Run(const fs::path& plan_file, const fs::path& output) {
  io::Json plan;
  Check(io::ParseJson(Read(plan_file, 65536), &plan, false,
                      {.reject_duplicate_keys = true}));
  const auto* cases = plan.GetArray("cases");
  Require(cases && cases->array.size() == 6, "six fixed tempo cases required");
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh output required");
  constexpr double kSpeeds[] = {0.25, 2, 4};
  size_t total_advances = 0;
  for (size_t index = 0; index < 6; ++index) {
    const auto& item = cases->array[index];
    const auto bytes = Read(item.GetString("pcm"), 160 * 1920 * 4);
    Require(bytes.size() % 4 == 0 &&
                trace::HexDigest(trace::Sha256(
                    {reinterpret_cast<const uint8_t*>(bytes.data()),
                     bytes.size()})) == item.GetString("pcm_sha256"),
            "speech fixture identity differs");
    std::vector<float> input(bytes.size() / 4);
    std::memcpy(input.data(), bytes.data(), bytes.size());
    Require(input.size() == (index < 3 ? 76800 : 74880),
            "original speech length differs");
    const double speed = item.GetNumber("speed");
    Require(speed == kSpeeds[index % 3], "tempo case speed differs");
    const size_t expected = std::ceil(input.size() / speed);
    audio::SpeechTempo tempo;
    Check(tempo.Init(speed, input.size(), expected));
    size_t fed = 0, progressive = 0, maximum_buffered = 0;
    std::vector<float> pcm;
    pcm.reserve(expected);
    for (;;) {
      Require(++total_advances <= 20000, "tempo advance budget exhausted");
      audio::SpeechTempoStep step;
      std::span<const float> values;
      Check(tempo.Advance(&step, &values));
      maximum_buffered = std::max(maximum_buffered, tempo.buffered_samples());
      if (step == audio::SpeechTempoStep::kDone) break;
      if (step == audio::SpeechTempoStep::kNeedInput) {
        Require(fed < input.size(), "tempo requests nonexistent input");
        const size_t count = std::min(size_t{4096}, input.size() - fed);
        Check(tempo.Append(std::span(input).subspan(fed, count),
                           fed + count == input.size()));
        fed += count;
      } else {
        Require(values.size() > 0 && values.size() <= 240,
                "tempo output step exceeds budget");
        if (fed < input.size()) ++progressive;
        pcm.insert(pcm.end(), values.begin(), values.end());
      }
    }
    Require(pcm.size() == expected && progressive > 0,
            "tempo length or progressive delivery failed");
    std::string wave;
    Check(audio::EncodeSpeechPcm(pcm, true, expected * 2 + 44, &wave));
    const auto directory = output / ("case-" + std::to_string(index));
    Require(fs::create_directory(directory), "fresh tempo case required");
    Save(directory / "audio.wav", wave);
    Save(directory / "pcm.f32", {reinterpret_cast<const char*>(pcm.data()),
                                 pcm.size() * sizeof(float)});
    Save(directory / "result.json",
         "{\"input_samples\":" + std::to_string(input.size()) +
             ",\"samples\":" + std::to_string(pcm.size()) +
             ",\"sample_rate\":24000,\"speed\":" + std::to_string(speed) +
             ",\"progressive_chunks\":" + std::to_string(progressive) +
             ",\"maximum_buffered_samples\":" +
             std::to_string(maximum_buffered) + "}\n");
  }
  Save(output / "result.json",
       "{\"status\":\"PASS_TEMPO_PENDING_CONTENT\","
       "\"cases\":6,\"model_loads\":0,\"advances\":" +
           std::to_string(total_advances) + "}\n");
}
}  // namespace

int main(int argc, char** argv) {
  try {
    Require(argc == 3, "usage: q4t_speech_tempo PLAN OUTPUT");
    Run(argv[1], argv[2]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
