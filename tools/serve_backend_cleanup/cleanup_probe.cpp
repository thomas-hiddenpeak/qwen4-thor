// Tiny real CUDA contracts, always run under the complete stage GPU lease.
#include <cuda_runtime.h>

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>

#include "q4t/mtp/mtp.h"
#include "q4t/runtime/cleanup_policy.h"
#include "q4t/runtime/startup_plan.h"
#include "q4t/runtime/startup_reservation.h"
#include "q4t/server/device_memory.h"
#include "../../src/server/cuda_memory_backend.h"

namespace {
void Check(bool ok, const char* what) {
  if (ok) return;
  std::fprintf(stderr, "probe failure: %s\n", what);
  std::_Exit(2);
}
void Cuda(cudaError_t error) { Check(error == cudaSuccess, "CUDA operation"); }

q4t::mtp::MtpConfig Tiny() {
  q4t::mtp::MtpConfig cfg;
  cfg.hs = cfg.hc = cfg.vocab = cfg.max_prefill = cfg.max_len = 1;
  return cfg;
}
}  // namespace

int main(int argc, char** argv) {
  Check(argc == 2 || argc == 3, "mode [new fixture directory]");
  Cuda(cudaSetDevice(0));
  const std::string mode = argv[1];
  if (mode == "all") {
    cudaStream_t stream = nullptr;
    cudaEvent_t event = nullptr;
    void* device = nullptr;
    void* host = nullptr;
    void* async = nullptr;
    Cuda(cudaStreamCreate(&stream));
    Cuda(cudaEventCreateWithFlags(&event, cudaEventDisableTiming));
    Cuda(cudaMalloc(&device, 4096));
    Cuda(cudaMallocHost(&host, 4096));
    Cuda(cudaMallocAsync(&async, 4096, stream));
    Cuda(cudaMemsetAsync(device, 0, 4096, stream));
    Cuda(cudaEventRecord(event, stream));
    const q4t::runtime::CleanupPhase phase("probe");
    Cuda(cudaEventSynchronize(event));
    Cuda(cudaStreamSynchronize(stream));
    Cuda(cudaDeviceSynchronize());
    Cuda(cudaFreeAsync(async, stream));
    // Successful async free/destroy is not a completion barrier.
    Cuda(cudaStreamSynchronize(stream));
    Cuda(cudaEventDestroy(event));
    Cuda(cudaStreamDestroy(stream));
    Cuda(cudaFreeHost(host));
    Cuda(cudaFree(device));
  } else if (mode == "handled-release") {
    void* device = nullptr;
    Cuda(cudaMalloc(&device, 4096));
    void* retained = device;
    const q4t::runtime::CleanupPhase phase("probe");
    const auto result = q4t::server::detail::ReleaseCudaMemory(device, "probe");
    Check(!device && result.state == q4t::server::DeviceMemoryState::kFailure &&
              result.operation_error == cudaErrorInvalidValue,
          "handled release must return original failure exactly once");
    Cuda(cudaDeviceSynchronize());
    // Only this fixture knows the injected call skipped the real free. A real
    // unknown-result owner must never retry it; process termination owns it.
    (void)retained;
  } else if (mode == "mtp-scratch") {
    q4t::mtp::MtpModel model;
    model.cfg = Tiny();
    Check(q4t::mtp::MtpReserveScratch(model, 2).ok(), "scratch reserve");
    Check(q4t::mtp::MtpReserveScratch(model, 3).ok(), "scratch grow");
    model.Free();
  } else if (mode == "mtp-load") {
    Check(argc == 3, "MTP fixture required");
    const std::filesystem::path fixture(argv[2]);
    Check(std::filesystem::create_directory(fixture), "fresh fixture");
    {
      std::ofstream index(fixture / "model.safetensors.index.json");
      index << "{\"weight_map\":{}}\n";
      index.close();
      Check(index.good(), "fixture write");
    }
    uint16_t* borrowed = nullptr;
    Cuda(cudaMalloc(reinterpret_cast<void**>(&borrowed), 2));
    q4t::mtp::MtpModel model;
    auto cfg = Tiny();
    cfg.mtp_dir = fixture.string();
    Check(!q4t::mtp::LoadMtp(cfg, borrowed, borrowed, &model, nullptr).ok(),
          "empty index must fail");
    Check(!model.fc_embedding && !model.embed_tokens, "failed-load cleanup");
    model.Free();
    Cuda(cudaFree(borrowed));
  } else if (mode == "reservation-release") {
    q4t::runtime::StartupPlan plan;
    Check(plan.AddBytes("unhanded", 4096).ok(), "plan");
    q4t::runtime::StartupReservation owner;
    Check(owner.Prepare(plan).ok(), "reserve");
    const q4t::runtime::CleanupPhase phase("probe");
    const auto result = owner.Finalize();
    // Reclaiming an unhanded allocation succeeds, but a prepared plan that
    // never handed out its entries must still report incomplete startup.
    Check(result.ownership.result.error ==
              q4t::runtime::reservation::Error::kPlanIncomplete &&
              result.ownership.result.backend_rc == 0 &&
              result.ownership.allocated == 1 && result.ownership.freed == 1 &&
              result.ownership.unhanded_remaining == 0 &&
              result.ownership.handed_live == 0 &&
              result.ownership.release_unknown == 0 &&
              result.ownership.finalized && !result.active,
          "unhanded reclaim must preserve incomplete-plan failure");
  } else {
    Check(false, "unknown mode");
  }
  std::printf("cleanup probe PASS: %s\n", mode.c_str());
}
