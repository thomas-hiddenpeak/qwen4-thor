// Fixed-reference ICL synthesis, full audio and bounded lifecycle checks.
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <utility>

#include "q4t/audio/tts_synthesis.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/tts_service.h"
#include "q4t/server/tts_upload.h"
#include "q4t/trace/sha256.h"

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
std::vector<uint8_t> Input(const io::Json& item, const char* name,
                            size_t bytes) {
  const auto path = fs::path(item.GetString(name));
  Require(path.is_absolute() && fs::is_regular_file(path) &&
              !fs::is_symlink(path) && fs::file_size(path) == bytes,
          "fixed ICL input size/path differs");
  std::ifstream file(path, std::ios::binary);
  std::vector<uint8_t> data(bytes);
  file.read(reinterpret_cast<char*>(data.data()), bytes);
  Require(file.good() &&
              trace::HexDigest(trace::Sha256(data)) ==
                  item.GetString(std::string(name) + "_sha256"),
          "fixed ICL input identity differs");
  return data;
}

audio::TtsPrompt Prompt(text::Tokenizer& tokenizer, const io::Json& item) {
  std::vector<uint32_t> text, instruction, reference;
  Check(tokenizer.Encode(item.GetString("text"), &text,
                         {.recognize_special_tokens = false}));
  Check(tokenizer.Encode(item.GetString("instruction"), &instruction,
                         {.recognize_special_tokens = false}));
  Check(tokenizer.Encode(item.GetString("reference_text"), &reference,
                         {.recognize_special_tokens = false}));
  const auto speaker = Input(item, "speaker_embedding", 2048 * sizeof(float));
  const auto raw_codes = Input(item, "reference_codes", 30 * 16 * 4);
  std::vector<float> condition(2048);
  std::vector<audio::TtsCodes> codes(30);
  std::memcpy(condition.data(), speaker.data(), speaker.size());
  std::memcpy(codes.data(), raw_codes.data(), raw_codes.size());
  audio::TtsPrompt result;
  Check(audio::PlanTtsBaseIclPrompt(
      std::vector<int32_t>(text.begin(), text.end()),
      std::vector<int32_t>(reference.begin(), reference.end()), codes,
      std::vector<int32_t>(instruction.begin(), instruction.end()), condition,
      item.GetString("language"), item.GetBool("non_streaming"), 512, &result));
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
    return Clock::now() - start > std::chrono::seconds(600);
  };
  const auto checkpoint = [&] {
    Require(!expired() && ++advances <= 100000,
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
    Check(server::PlanTtsServiceResources(
        {}, 512, 512, 160, &resources, audio::TtsVariant::kBase17B, 30));
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
    Check(server::LoadTtsAssets(directory, &assets, control, &failure, true,
                                server::kTtsAssetStagingBytes,
                                {audio::TtsVariant::kBase17B, true}));
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
    for (size_t index = 0; index < 5; ++index) {
      const bool cancellation = index == 2;
      const bool frame_limit = index >= 3;
      const bool deferred = index == 1 || index == 4;
      const size_t max_frames = frame_limit ? (deferred ? 2 : 3) : 160;
      const auto prompt = Prompt(*assets.tokenizer, cases->array[index % 2]);
      audio::TtsGenerationLayout generation;
      audio::TtsCodecLayout waveform;
      Check(audio::PlanTtsGenerationWorkspace(
          audio::TtsVariant::kBase17B, prompt.text_ids.size(),
          prompt.prefill.size(), max_frames, 512ULL << 20, &generation,
          prompt.reference_codes.size()));
      Check(audio::PlanTtsCodecWorkspace(std::min(max_frames + 30, size_t{41}),
                                         1ULL << 30, &waveform));
      Device generation_device(generation.bytes), codec_device(waveform.bytes);
      audio::TtsSynthesisRun run;
      const audio::TtsSampling sampling;
      Check(run.Begin(weights.talker, weights.codec, prompt, max_frames, 16,
                      sampling, sampling, 0, generation_device.Span(),
                      codec_device.Span(), nullptr, deferred));
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
        Require(early_output && run.frames() == 2 && run.emitted_frames() == 0,
                "cancellation did not cover a real pending output");
        continue;
      }
      if (frame_limit) {
        Require(budget_failed && run.frames() == max_frames &&
                    pcm.size() == (deferred ? size_t{0} : size_t{2} * 1920) &&
                    run.emitted_frames() == (deferred ? size_t{0} : size_t{2}),
                "frame limit converted into EOS or discarded committed output");
        continue;
      }
      Require(
          run.done() && run.ended() && !run.at_limit() &&
              early_output == !deferred &&
              run.frames() == run.emitted_frames() &&
              chunk_frames.size() >= 2 &&
              pcm.size() == run.frames() * audio::TtsConfig::kSamplesPerFrame,
          "incremental synthesis completion differs");
      Require(chunk_frames.front() == 2,
              "first mixed global chunk lost or repeated reference samples");
      for (size_t i = 1; i + 1 < chunk_frames.size(); ++i)
        Require(chunk_frames[i] == 16, "global codec chunk grid changed");
      for (auto sample : pcm)
        Require(std::isfinite(sample) && std::abs(sample) <= 1,
                "nonfinite or unnormalized PCM");
      const auto path = output / ("case-" + std::to_string(index));
      Require(fs::create_directory(path), "duplicate case");
      Save<float>(path / "pcm.f32", pcm);
      Save<audio::TtsCodes>(path / "codes.i32", run.codes());
      std::ofstream receipt(path / "result.json");
      receipt << "{\"eos\":true,\"at_limit\":false,\"early_output\":"
              << (early_output ? "true" : "false")
              << ",\"deferred\":" << (deferred ? "true" : "false")
              << ",\"reference_frames\":30,\"frames\":" << run.frames()
              << ",\"samples\":" << pcm.size()
              << ",\"sample_rate\":24000,\"chunks\":[";
      for (size_t i = 0; i < chunk_frames.size(); ++i)
        receipt << (i ? "," : "") << chunk_frames[i];
      receipt << "]}\n";
      receipt.close();
      Require(receipt.good(), "receipt write failed");
    }
  }
  Require(!cleanup_failed && allocated == released && allocated == 12,
          "device resources not released");
  std::ofstream receipt(output / "result.json");
  receipt << "{\"status\":\"PASS_ICL_SYNTHESIS_PENDING_CONTENT\",\"cases\":2,"
          << "\"cancellation_and_two_frame_limits\":true,\"gpu_talker_loads\":1,"
          << "\"gpu_codec_loads\":1,\"asset_workers_reaped\":2,"
          << "\"sampling_policy\":\"checkpoint-default-seed0\","
          << "\"encoder_loads\":0,"
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
    Require(argc == 4, "usage: icl_synthesis ASSETS PLAN OUTPUT");
    Execute(fs::absolute(argv[1]), fs::absolute(argv[2]),
            fs::absolute(argv[3]));
    return 0;
  } catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
