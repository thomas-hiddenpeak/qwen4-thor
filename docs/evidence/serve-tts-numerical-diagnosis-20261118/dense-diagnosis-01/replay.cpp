#define main TtsQualificationMain
#include "tools/native_tts/model_replay.cpp"
#undef main

int main(int argc, char** argv) {
  try {
    Require(argc == 4, "expected ASSETS PROJECTED OUTPUT");
    const char* lease = std::getenv("Q4T_MANAGED_LEASE_FD");
    Require(lease && *lease, "inherited lease required");
    const int fd = std::stoi(lease);
    Require(!flock(fd, LOCK_EX | LOCK_NB), "lease unavailable");
    const fs::path assets = argv[1], input = argv[2], output = argv[3];
    Require(fs::create_directories(output), "fresh output required");
    trace_directory = output;
    auto file = ReadFile(assets / "model.safetensors", 3833402552);
    Require(trace::HexDigest(trace::Sha256(file)) ==
      "38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137",
      "model identity mismatch");
    uint64_t header;
    std::memcpy(&header, file.data(), 8);
    constexpr auto variant = audio::TtsVariant::kCustomVoice17B;
    audio::TtsWeightLayout layout;
    Check(audio::ParseTtsWeightLayout(Json(std::string(
      reinterpret_cast<const char*>(file.data() + 8), header)),
      file.size() - header - 8, variant, &layout));
    Device data(layout.bytes);
    for (const auto& tensor : layout.tensors)
      Cuda(cudaMemcpy(static_cast<uint8_t*>(data.value) + tensor.offset,
        file.data() + 8 + header + tensor.file_offset, tensor.bytes,
        cudaMemcpyHostToDevice));
    std::vector<uint8_t>().swap(file);
    audio::TtsDeviceWeights weights;
    Check(audio::BindTtsDeviceWeights(data.value, layout.bytes,
                                     layout, variant, &weights));
    auto projected = ReadFile(input, 4096);
    Require(projected.size() == 4096, "exactly two projected rows");
    Device inputs(4096);
    Cuda(cudaMemcpy(inputs.value, projected.data(), 4096, cudaMemcpyHostToDevice));
    audio::TtsDenseLayout workspace;
    Check(audio::PlanTtsDenseWorkspace(variant, true, 2, 16, 64ULL << 20,
                                      &workspace));
    Device scratch(workspace.bytes);
    audio::TtsDenseRun run;
    Check(run.Init(weights.predictor, variant, true, 2, 16,
                    scratch.value, workspace.bytes));
    Check(run.Begin({static_cast<const uint16_t*>(inputs.value), 2048}));
    for (int layer = 0; layer < 5; ++layer) Check(run.NextLayer());
    std::span<const uint16_t> last;
    Check(run.Finish(&last));
    audio::TraceTtsOperand("diag-final", 0, 0, last);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
