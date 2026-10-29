// Re-run the existing storage contracts on the new path, then exercise the
// writer/producer handoff with deterministic barriers, not timing guesses.
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <thread>

#define main PreviousMain
#define __wrap_pwrite PreviousWrite
#include "../session_preallocate/storage_contract.cpp"
#undef __wrap_pwrite
#undef main

namespace {
std::atomic<int> pipe_fault{0}, writes{0}, finished{0};
std::mutex barrier_mutex;
std::condition_variable barrier_cv;
bool started = false, release_write = false;
std::atomic<bool> timed_out{false};
}

extern "C" ssize_t __wrap_pwrite(int fd, const void* data, size_t n,
                                 off_t offset) {
  const int mode = pipe_fault.load();
  if (!mode) return PreviousWrite(fd, data, n, offset);
  const int call = ++writes;
  if (mode == 4 && call == 1) {
    errno = EINTR;
    return -1;
  }
  if (mode == 5 && call == 1) {
    std::unique_lock lock(barrier_mutex);
    started = true;
    barrier_cv.notify_one();
    if (!barrier_cv.wait_for(lock, std::chrono::seconds(5),
                            [] { return release_write; })) {
      timed_out = true;
      errno = EIO;
      return -1;
    }
  }
  if (mode == 6 && call == 2) n /= 2;
  const ssize_t result = __real_pwrite(fd, data, n, offset);
  ++finished;
  return result;
}

int main(int argc, char** argv) {
  if (PreviousMain(argc, argv)) return 1;
  using namespace q4t;
  using namespace q4t::server;
  int checks = 0;
  auto require = [&](bool ok, const char* message) {
    ++checks;
    if (!ok) throw std::runtime_error(message);
  };
  try {
    setenv("Q4T_SESSION_PREALLOCATE", "0", 1);
    SessionCache cache;
    require(cache.Open(argv[1]).ok(), "pipeline open");
    std::vector<int32_t> prefix(8193, 42);
    std::vector<uint8_t> data(2 * SessionCache::kStagingBytes + 99);
    for (size_t i = 0; i < data.size(); ++i) data[i] = i * 31 + i / 7919;
    const auto expected = data;
    std::vector<SessionRange> ranges{{data.data(), data.size() - 71},
                                    {data.data() + data.size() - 71, 71}};
    const auto copy = [](void* host, void* state, size_t bytes, bool restore) {
      std::memcpy(restore ? state : host, restore ? host : state, bytes);
      return Status();
    };
    const auto no_cancel = [] { return false; };
    for (const auto& [mode, cancel_case] :
         {std::pair{4, false}, {6, false}, {5, false}, {5, true}, {4, false}}) {
      data = expected;
      require(cache.Acquire("pipe"), "pipeline lease");
      writes = 0;
      finished = 0;
      started = false;
      release_write = false;
      bool cancelled = false;
      int copies = 0;
      pipe_fault = mode;
      const auto checked_copy = [&](void* host, void* state, size_t bytes,
                                    bool restore) {
        ++copies;
        if (mode == 5 && copies == 2) {
          std::unique_lock lock(barrier_mutex);
          const bool in_flight = barrier_cv.wait_for(
              lock, std::chrono::seconds(5), [] { return started; });
          release_write = true;
          barrier_cv.notify_one();
          if (!in_flight) return Status::Fail("no concurrent write");
          if (!cancel_case) return Status::Fail("copy failed during write");
          cancelled = true;
        }
        return copy(host, state, bytes, restore);
      };
      SessionSaveTiming timing("pipeline-contract", "private");
      const Status result = cache.Save("pipe", prefix, ranges, checked_copy,
          [&] { return cancelled; }, 248000, &timing);
      pipe_fault = 0;  // Save must have joined the writer before this change.
      timing.Report(result.ok());
      require(!timed_out, "writer barrier timeout");
      require(timing.pipeline, "pipeline actually selected");
      require(result.ok() == (mode == 4), "expected write/copy/cancel result");
      require(!cache.Match("pipe", prefix), "no early publication");
      require(std::distance(std::filesystem::directory_iterator(
                  "/proc/self/task"), std::filesystem::directory_iterator{})
                  == 1, "no writer survives Save");
      if (mode == 5) require(finished == 1, "in-flight write drained");
      if (mode == 4) {
        require(writes == 5 && finished == 4, "EINTR plus four blocks");
        cache.Release("pipe", true);
        require(cache.Acquire("pipe"), "restore lease");
        std::fill(data.begin(), data.end(), 0);
        int32_t d0 = -1, pending = -1;
        require(cache.Restore("pipe", ranges, copy, no_cancel, &d0,
                              &pending).ok(), "multi-range restore");
        require(data == expected && d0 == 248000 && pending == 42,
                "exact bytes/metadata with rotating buffers");
      } else {
        require(cache.Bytes() == 0, "failed replacement removes entry");
      }
      cache.Release("pipe", false);
    }
    std::printf("PASS pipeline_checks=%d extra_staging=%zu\n", checks,
                SessionCache::kStagingBytes);
    return 0;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "FAIL pipeline_checks=%d error=%s\n", checks,
                  error.what());
    return 1;
  }
}
