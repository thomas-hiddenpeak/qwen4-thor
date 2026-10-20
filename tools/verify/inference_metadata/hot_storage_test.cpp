// Delayed D2H writes exercise success and early-exit destination ownership.
#include "q4t/io/pending_host_storage.h"

#include <cstring>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
struct Storage {
  std::vector<int> ids{0, 0};
  std::vector<float> weights{0, 0};
  static inline int alive = 0;
  Storage() { ++alive; }
  ~Storage() { --alive; }
};
Storage* queued = nullptr;
cudaStream_t expected_stream = nullptr;
int failures = 0;
void Require(bool value) {
  if (!value) throw std::runtime_error("D2H destination contract failed");
}
void Complete() {
  Require(Storage::alive == 1 && queued);
  queued->ids = {3, 7};
  queued->weights = {0.25f, 0.75f};
  queued = nullptr;
}
}  // namespace

extern "C" cudaError_t cudaStreamSynchronize(cudaStream_t stream) {
  Require(stream == expected_stream && Storage::alive == 1);
  if (failures > 0) {
    --failures;
    return cudaErrorUnknown;
  }
  Complete();
  return cudaSuccess;
}
extern "C" const char* cudaGetErrorString(cudaError_t) {
  return "injected failure";
}

int main() {
  try {
    int cases = 0;
    for (auto stream : {cudaStream_t{},
                        reinterpret_cast<cudaStream_t>(uintptr_t{1})}) {
      expected_stream = stream;
      {
        q4t::io::PendingHostStorage<Storage> owner(stream);
        queued = &owner.Get();
        owner.MarkPending();
        Require(owner.Drain().ok());
        Require(owner.Get().ids == std::vector<int>({3, 7}));
        Require(owner.Get().weights == std::vector<float>({0.25f, 0.75f}));
      }
      Require(Storage::alive == 0 && !queued);
      ++cases;
      // An API failure after possible submission takes the destructor path.
      {
        q4t::io::PendingHostStorage<Storage> owner(stream);
        queued = &owner.Get();
        owner.MarkPending();
      }
      Require(Storage::alive == 0 && !queued);
      ++cases;
      {
        q4t::io::PendingHostStorage<Storage> owner(stream);
        queued = &owner.Get();
        owner.MarkPending();
        failures = 1;
        Require(!owner.Drain().ok());
      }
      Require(Storage::alive == 0 && !queued);
      ++cases;
      Storage* retained = nullptr;
      {
        q4t::io::PendingHostStorage<Storage> owner(stream);
        retained = queued = &owner.Get();
        owner.MarkPending();
        failures = 2;
        Require(!owner.Drain().ok());
      }
      Require(Storage::alive == 1 && queued == retained);
      // Simulated context recovery belongs to the test, not the owner API.
      Complete();
      Require(retained->ids == std::vector<int>({3, 7}));
      delete retained;
      Require(Storage::alive == 0);
      ++cases;
    }
    std::cout << "PASS D2H ownership cases=" << cases << '\n';
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
