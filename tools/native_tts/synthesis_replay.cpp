// Production paired loader, bounded upload and incremental synthesis replay.
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <utility>

#include "q4t/audio/tts_synthesis.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/tts_service.h"
#include "q4t/server/tts_upload.h"

#ifdef Q4T_TTS_TRACE
#error "Synthesis lifecycle qualification uses the production no-trace graph"
#endif

namespace {
using namespace q4t;
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
size_t allocated = 0, released = 0, advances = 0;
bool cleanup_failed = false;
void Require(bool ok, const std::string& message) {
  if (!ok) throw std::runtime_error(message);
}
void Check(const Status& s) { Require(s.ok(), s.message()); }
void Check(const audio::TtsComputeResult& r) {
  Require(r.ok(),
          r.status.message() + ": " + cudaGetErrorString(r.device_error));
}
void Cuda(cudaError_t error) {
  Require(error == cudaSuccess, cudaGetErrorString(error));
}
struct Device {
  uint8_t* pointer = nullptr;
  size_t bytes;
  explicit Device(size_t count) : bytes(count) {
    void* raw = nullptr;
    Cuda(cudaMalloc(&raw, bytes));
    pointer = static_cast<uint8_t*>(raw);
    ++allocated;
  }
  ~Device() {
    if (cudaFree(std::exchange(pointer, nullptr)) == cudaSuccess)
      ++released;
    else
      cleanup_failed = true;
  }
  Device(const Device&) = delete;
  std::span<uint8_t> Span() const { return {pointer, bytes}; }
};
io::Json ReadJson(const fs::path& path) {
  Require(fs::file_size(path) < 65536, "plan size exceeds limit");
  std::ifstream file(path);
  std::string text((std::istreambuf_iterator<char>(file)), {});
  io::Json result;
  Check(io::ParseJson(text, &result, false, {.reject_duplicate_keys = true}));
  return result;
}
template <typename T>
void Save(const fs::path& path, std::span<const T> values) {
  std::ofstream file(path, std::ios::binary);
  file.write(reinterpret_cast<const char*>(values.data()), values.size_bytes());
  file.close();
  Require(file.good(), "artifact write failed");
}
audio::TtsPrompt Prompt(text::Tokenizer& tokenizer, const io::Json& item) {
  std::vector<uint32_t> text, instruction;
  Check(tokenizer.Encode(item.GetString("text"), &text,
                         {.recognize_special_tokens = false}));
  Check(tokenizer.Encode(item.GetString("instruction"), &instruction,
                         {.recognize_special_tokens = false}));
  audio::TtsPrompt result;
  Check(audio::PlanTtsPrompt(
      audio::TtsVariant::kCustomVoice17B,
      std::vector<int32_t>(text.begin(), text.end()),
      std::vector<int32_t>(instruction.begin(), instruction.end()),
      item.GetString("voice"), item.GetString("language"),
      item.GetBool("non_streaming"), 512, &result));
  return result;
}

void Execute(const fs::path& directory, const fs::path& plan_file,
             const fs::path& output) {
  const auto* inherited = std::getenv("Q4T_MANAGED_LEASE_FD");
  Require(inherited && *inherited, "strict inherited lease required");
  char* end;
  const auto lease = std::strtol(inherited, &end, 10);
  struct stat st {};
  Require(!*end && lease >= 0 && lease < 1048576 && !fstat(lease, &st) &&
              S_ISREG(st.st_mode) && !flock(lease, LOCK_EX | LOCK_NB),
          "invalid inherited lease");
  const auto start = Clock::now();
  const auto expired = [&] {
    return Clock::now() - start > std::chrono::seconds(180);
  };
  const auto checkpoint = [&] {
    Require(!expired() && ++advances <= 75000,
            "synthesis step/deadline budget");
  };
  const auto plan = ReadJson(plan_file);
  const auto* cases = plan.GetArray("cases");
  Require(cases && cases->array.size() == 2, "two fixed utterances required");
  Require(!fs::exists(output) && fs::create_directories(output),
          "fresh output required");
  auto counters = std::make_shared<server::GrammarWorkerCounters>();
  {
    server::TtsResourcePlan resources;
    Check(server::PlanTtsServiceResources({}, 512, 512, 160, &resources));
    Require(runtime::ReadSystemMemory().CanAllocate(
                resources.load_peak_bytes + resources.request_device_bytes +
                (32ULL << 20)),
            "synthesis unified RAM unavailable");
    server::TtsHostAssets assets;
    server::GrammarFailure failure;
    server::GrammarControl control;
    control.cancelled = expired;
    control.compile_deadline = start + std::chrono::seconds(60);
    control.counters = counters;
    Check(server::LoadTtsAssets(directory, &assets, control, &failure));
    Require(counters->started == 2 && counters->reaped == 2 &&
                counters->active == 0,
            "asset children not reaped");
    Device talker(server::kNativeTtsWeightBytes);
    Device codec(audio::TtsConfig::kCodecDecoderBytes);
    server::TtsBoundWeights weights;
    // Cancellation before the first transfer cannot publish ready bindings.
    const auto cancelled = server::UploadTtsWeights(
        assets, talker.Span(), codec.Span(), &weights, [] { return true; });
    Require(!cancelled.ok() && cancelled.device_error == cudaSuccess &&
                !weights.talker.text_embedding &&
                !weights.codec.pre_conv.weight,
            "cancelled upload published bindings");
    Check(server::UploadTtsWeights(assets, talker.Span(), codec.Span(),
                                   &weights, expired));
    std::vector<uint8_t>().swap(assets.talker.bytes);
    std::vector<uint8_t>().swap(assets.codec.bytes);
    for (size_t index = 0; index < 4; ++index) {
      const bool cancellation = index == 2;
      const bool frame_limit = index == 3;
      const size_t max_frames = frame_limit ? 2 : 160;
      const auto prompt = Prompt(*assets.tokenizer, cases->array[index % 2]);
      audio::TtsGenerationLayout generation;
      audio::TtsCodecLayout waveform;
      Check(audio::PlanTtsGenerationWorkspace(
          audio::TtsVariant::kCustomVoice17B, prompt.text_ids.size(),
          prompt.prefill.size(), max_frames, 512ULL << 20, &generation));
      Check(audio::PlanTtsCodecWorkspace(std::min(max_frames, size_t{41}),
                                         1ULL << 30, &waveform));
      Device generation_device(generation.bytes), codec_device(waveform.bytes);
      audio::TtsSynthesisRun run;
      Check(run.Begin(weights.talker, weights.codec, prompt, max_frames, 16,
                      {.sample = false},
                      {.sample = false, .repetition_penalty = 1.0f}, 42,
                      generation_device.Span(), codec_device.Span()));
      std::vector<float> pcm;
      pcm.reserve(max_frames * audio::TtsConfig::kSamplesPerFrame);
      std::vector<size_t> chunk_frames;
      bool early_output = false, budget_failed = false;
      while (!run.done()) {
        checkpoint();
        const auto result = run.Advance();
        if (!result.ok()) {
          Require(frame_limit && run.at_limit() && run.failed() &&
                      !run.ended() && result.device_error == cudaSuccess,
                  result.status.message());
          budget_failed = true;
          break;
        }
        if (!run.output_ready()) continue;
        early_output |= !run.ended();
        const auto chunk = run.OutputDevicePcm();
        const auto frames_before = run.frames();
        Require(!run.Advance().ok() && run.output_ready() && !run.failed() &&
                    run.frames() == frames_before &&
                    chunk.data() == run.OutputDevicePcm().data(),
                "unacknowledged PCM was overwritten");
        if (cancellation) {
          run.Cancel();
          Require(run.failed() && !run.done() &&
                      run.OutputDevicePcm().empty() &&
                      !run.AcknowledgeOutput() && !run.Advance().ok(),
                  "cancelled synthesis remained executable");
          break;
        }
        const auto offset = pcm.size();
        pcm.resize(offset + chunk.size());
        Cuda(cudaMemcpy(pcm.data() + offset, chunk.data(), chunk.size_bytes(),
                        cudaMemcpyDeviceToHost));
        chunk_frames.push_back(chunk.size() /
                               audio::TtsConfig::kSamplesPerFrame);
        Check(run.AcknowledgeOutput());
        Require(run.OutputDevicePcm().empty() && !run.AcknowledgeOutput(),
                "duplicate acknowledgement accepted");
      }
      if (cancellation) {
        Require(early_output && run.frames() == 16 && run.emitted_frames() == 0,
                "cancellation did not cover a real pending output");
        continue;
      }
      if (frame_limit) {
        Require(budget_failed && run.frames() == 2 && pcm.empty(),
                "frame limit converted into successful EOS");
        continue;
      }
      Require(
          run.done() && run.ended() && !run.at_limit() && early_output &&
              run.frames() == run.emitted_frames() &&
              chunk_frames.size() >= 2 &&
              pcm.size() == run.frames() * audio::TtsConfig::kSamplesPerFrame,
          "incremental synthesis completion differs");
      for (auto sample : pcm)
        Require(std::isfinite(sample) && std::abs(sample) <= 1,
                "nonfinite or unnormalized PCM");
      const auto path = output / ("case-" + std::to_string(index));
      Require(fs::create_directory(path), "duplicate case");
      Save<float>(path / "pcm.f32", pcm);
      Save<audio::TtsCodes>(path / "codes.i32", run.codes());
      std::ofstream receipt(path / "result.json");
      receipt << "{\"eos\":true,\"at_limit\":false,\"early_output\":true,"
              << "\"frames\":" << run.frames() << ",\"samples\":" << pcm.size()
              << ",\"sample_rate\":24000,\"chunks\":[";
      for (size_t i = 0; i < chunk_frames.size(); ++i)
        receipt << (i ? "," : "") << chunk_frames[i];
      receipt << "]}\n";
      receipt.close();
      Require(receipt.good(), "receipt write failed");
    }
  }
  Require(!cleanup_failed && allocated == released && allocated == 10,
          "device resources not released");
  std::ofstream receipt(output / "result.json");
  receipt << "{\"status\":\"PASS_SYNTHESIS_PENDING_CONTENT\",\"cases\":2,"
          << "\"cancellation_and_frame_limit\":true,\"gpu_talker_loads\":1,"
          << "\"gpu_codec_loads\":1,\"asset_workers_reaped\":2,"
          << "\"allocated\":" << allocated << ",\"released\":" << released
          << ",\"advances\":" << advances << "}\n";
  receipt.close();
  Require(receipt.good(), "completion receipt failed");
}
}  // namespace

int main(int argc, char** argv) {
  const int worker = q4t::server::MaybeRunModelAssetWorker(argc, argv);
  if (worker >= 0) return worker;
  try {
    Require(argc == 4, "usage: synthesis ASSETS PLAN OUTPUT");
    Execute(fs::absolute(argv[1]), fs::absolute(argv[2]),
            fs::absolute(argv[3]));
    return 0;
  } catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
