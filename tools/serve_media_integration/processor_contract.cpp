// Actual PNG/JPEG processing under bounded decoder ownership and cooperative
// cancellation. Wrapped C/C++ allocations are checked empty on every exit.
#include "q4t/vision/media_plan.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <new>
#include <string>
#include <thread>
#include <vector>

namespace {
thread_local bool tracking = false;
thread_local size_t c_calls = 0;
thread_local std::array<void*, 4096> live{};
void Require(bool condition, const char* why) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", why);
    std::exit(1);
  }
}
void Track(void* pointer) {
  if (!tracking || !pointer) return;
  const auto it = std::find(live.begin(), live.end(), nullptr);
  Require(it != live.end(), "ledger full");
  *it = pointer;
}
void Forget(void* pointer) {
  if (!pointer) return;
  const auto it = std::find(live.begin(), live.end(), pointer);
  if (it != live.end()) *it = nullptr;
}
}  // namespace
extern "C" void* __real_malloc(size_t);
extern "C" void* __real_realloc(void*, size_t);
extern "C" void __real_free(void*);
extern "C" void* __wrap_malloc(size_t size) {
  if (tracking) ++c_calls;
  void* result = __real_malloc(size);
  Track(result);
  return result;
}
extern "C" void* __wrap_realloc(void* pointer, size_t size) {
  if (tracking) ++c_calls;
  const auto it = std::find(live.begin(), live.end(), pointer);
  const size_t index = static_cast<size_t>(it - live.begin());
  void* result = __real_realloc(pointer, size);
  if (result || !size) {
    if (index < live.size()) live[index] = nullptr;
    Track(result);
  }
  return result;
}
extern "C" void __wrap_free(void* pointer) {
  Forget(pointer);
  __real_free(pointer);
}
void* operator new(size_t size) {
  // Bypass the C-only wrapper explicitly. Stores around builtin malloc can
  // be optimized away before the linker applies --wrap.
  void* pointer = __real_malloc(size ? size : 1);
  Track(pointer);
  if (!pointer) throw std::bad_alloc();
  return pointer;
}
void* operator new[](size_t size) { return ::operator new(size); }
void operator delete(void* pointer) noexcept { std::free(pointer); }
void operator delete[](void* pointer) noexcept { std::free(pointer); }
void operator delete(void* pointer, size_t) noexcept { std::free(pointer); }
void operator delete[](void* pointer, size_t) noexcept { std::free(pointer); }

namespace {
using namespace q4t::vision;
struct Cancel {
  size_t calls = 0, stop = 0;
};
struct Result {
  size_t peak, callbacks, c_allocations;
};

void CheckAllocationLedger() {
  tracking = true;
  c_calls = 0;
  void* (*volatile allocate)(size_t) = &::operator new;
  void* cpp = allocate(64);
  Require(c_calls == 0 && std::count(live.begin(), live.end(), cpp) == 1,
          "C++ allocation incorrectly counted as C");
  ::operator delete(cpp);
  void* c = __wrap_malloc(64);
  Require(c_calls == 1 && std::count(live.begin(), live.end(), c) == 1,
          "C allocation not counted");
  __wrap_free(c);
  tracking = false;
  Require(std::all_of(live.begin(), live.end(), [](auto* p) { return !p; }),
          "ledger self-check leaked");
  std::puts("PASS ledger distinguishes C/C++ allocation ownership");
}

Result Process(const std::vector<uint8_t>& bytes, size_t frame_count,
               size_t quota, size_t cancel_at, const char* expected,
               bool bad_tail = false, bool wrong_plan = false) {
  std::vector<const uint8_t*> frames(frame_count, bytes.data());
  std::vector<size_t> lengths(frame_count, bytes.size());
  const uint8_t invalid[] = {0};
  if (bad_tail) {
    frames.back() = invalid;
    lengths.back() = 1;
  }
  ProcessorResult detail;
  Cancel cancel{0, cancel_at};
  Result result{};
  ProcessorConfig cfg;
  if (frame_count > 1) {
    cfg.min_pixels = 4096;
    cfg.max_pixels = 25165824;
  }
  MediaItemPlan admission;
  if (wrong_plan) {
    MediaHeader header;
    Require(static_cast<bool>(ReadMediaHeader(bytes, &header)),
            "fixture header");
    admission = {MediaKind::kImage, header.width + 1, header.height, 1, {}};
  }
  tracking = true;
  c_calls = 0;
  {
    ProcessedImage image;
    ProcessedVideo video;
    std::string error;
    ProcessorOptions options{.decoder_owned_bytes = quota,
                             .cancelled =
                                 [](void* pointer) {
                                   auto& c = *static_cast<Cancel*>(pointer);
                                   ++c.calls;
                                   return c.stop && c.calls >= c.stop;
                                 },
                             .cancel_context = &cancel,
                             .admission = wrong_plan ? &admission : nullptr,
                             .result = &detail};
    const bool ok =
        frame_count == 1
            ? ProcessImage(bytes.data(), bytes.size(), cfg, &image, &error,
                           options)
            : ProcessVideo(frames, lengths, cfg, &video, &error, options);
    Require(std::string(detail.code) == expected, detail.code);
    Require(ok == (std::string(expected) == "ok"), "status mismatch");
    Require(detail.decoder_peak_bytes <= quota, "decoder exceeded quota");
    if (!ok)
      Require(image.pixel_values.empty() && video.pixel_values.empty(),
              "partial output survived failure");
    if (bad_tail)
      Require(detail.frame == frame_count - 1, "wrong failing frame");
    result = {detail.decoder_peak_bytes, cancel.calls, c_calls};
  }
  tracking = false;
  Require(std::all_of(live.begin(), live.end(), [](auto* p) { return !p; }),
          "allocation ownership leaked");
  return result;
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 3) return 2;
  CheckAllocationLedger();
  size_t calls = 0, cancellation_cases = 0, quota_cases = 0;
  for (int i = 1; i < argc; ++i) {
    std::ifstream in(argv[i], std::ios::binary);
    Require(static_cast<bool>(in), "fixture unavailable");
    std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(in)), {});
    for (size_t frames : {1u, 2u, 5u}) {
      const auto base = Process(bytes, frames, kDecoderOwnedBytes, 0, "ok");
      ++calls;
      Require(base.peak > 4096 && base.callbacks > 2 && base.callbacks < 64,
              "unexpected baseline work");
      for (size_t quota :
           {size_t{1}, size_t{32}, size_t{256}, size_t{4096}, base.peak - 1}) {
        Process(bytes, frames, quota, 0, "media_decode_limit");
        ++calls;
        ++quota_cases;
      }
      Process(bytes, frames, base.peak, 0, "ok");
      ++calls;
      for (size_t stop = 1; stop <= base.callbacks; ++stop) {
        const auto stopped = Process(bytes, frames, kDecoderOwnedBytes, stop,
                                     "request_cancelled");
        if (stop == 1) Require(!stopped.c_allocations, "cancel after decoding");
        ++calls;
        ++cancellation_cases;
      }
      if (frames > 1) {
        const auto invalid = Process(bytes, frames, kDecoderOwnedBytes, 0,
                                     "media_format_unsupported", true);
        Require(!invalid.c_allocations, "tail rejected after partial decoding");
        ++calls;
      }
      std::printf("PASS %s frames=%zu peak=%zu checkpoints=%zu\n", argv[i],
                  frames, base.peak, base.callbacks);
    }
    const auto wrong = Process(bytes, 1, kDecoderOwnedBytes, 0,
                               "media_plan_mismatch", false, true);
    Require(!wrong.c_allocations, "mismatch decoded before rejection");
    ++calls;
    Process(bytes, 1, 0, 0, "media_config_invalid");
    ++calls;
    std::thread denied([&] { Process(bytes, 5, 1, 0, "media_decode_limit"); });
    std::thread allowed(
        [&] { Process(bytes, 5, kDecoderOwnedBytes, 0, "ok"); });
    denied.join();
    allowed.join();
    calls += 2;
  }
  std::printf(
      "PASS %zu processor calls; %zu quota failures; %zu cancellation "
      "boundaries; ownership empty; concurrent budgets independent\n",
      calls, quota_cases, cancellation_cases);
  return 0;
}
