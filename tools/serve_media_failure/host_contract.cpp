// Real response/processor code under persistent C++ allocation denial.
// No driver/model/server; socketpair transport only. Wrapped C allocations
// count ownership during processor calls, including stb's decoded source.
#include "q4t/server/http_response.h"
#include "q4t/vision/processor.h"

#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <memory>
#include <new>
#include <string>
#include <vector>

namespace {
thread_local size_t deny_from = 0;
thread_local size_t cpp_calls = 0;
thread_local size_t denied = 0;
bool tracking = false;
thread_local bool cpp_malloc = false;
size_t c_calls = 0, c_deny_from = 0;
bool DenyC(size_t bytes) {
  if (!tracking || cpp_malloc || !bytes) return false;
  return ++c_calls >= c_deny_from && c_deny_from;
}
struct Calls {
  size_t cpp = 0, c = 0;
};
std::array<void*, 4096> live{};
int send_mode = 0;

void Check(bool condition, const char* why) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", why);
    std::exit(1);
  }
}
void Track(void* p) {
  if (!tracking || !p) return;
  auto it = std::find(live.begin(), live.end(), nullptr);
  Check(it != live.end(), "tracking capacity");
  *it = p;
}
void Forget(void* p) {
  if (!p) return;
  auto it = std::find(live.begin(), live.end(), p);
  if (it != live.end()) *it = nullptr;
}
void BeforeNew() {
  if (++cpp_calls >= deny_from && deny_from) {
    ++denied;
    throw std::bad_alloc();
  }
}
void Deny(size_t from) {
  cpp_calls = denied = 0;
  deny_from = from;
}
void Allow() { deny_from = 0; }
std::string Read(int fd) {
  std::string wire;
  char bytes[2048];
  for (;;) {
    const auto n = recv(fd, bytes, sizeof(bytes), 0);
    if (n <= 0) return wire;
    wire.append(bytes, static_cast<size_t>(n));
  }
}
}  // namespace

extern "C" void* __real_malloc(size_t);
extern "C" void* __real_realloc(void*, size_t);
extern "C" void __real_free(void*);
extern "C" void* __wrap_malloc(size_t bytes) {
  if (DenyC(bytes)) return nullptr;
  void* p = __real_malloc(bytes);
  Track(p);
  return p;
}
extern "C" void* __wrap_realloc(void* p, size_t bytes) {
  if (DenyC(bytes)) return nullptr;
  // Preserve the tracked old pointer when realloc fails.
  const auto it = std::find(live.begin(), live.end(), p);
  const size_t index = static_cast<size_t>(it - live.begin());
  void* result = __real_realloc(p, bytes);
  if (result || !bytes) {
    if (index < live.size()) live[index] = nullptr;
    Track(result);
  }
  return result;
}
extern "C" void __wrap_free(void* p) {
  Forget(p);
  __real_free(p);
}
extern "C" ssize_t __real_send(int, const void*, size_t, int);
extern "C" ssize_t __wrap_send(int fd, const void* data, size_t n, int flags) {
  if (send_mode == 1) {
    send_mode = 2;
    return __real_send(fd, data, std::min(n, size_t{8}), flags);
  }
  if (send_mode == 2) {
    errno = EPIPE;
    return -1;
  }
  return __real_send(fd, data, n, flags);
}

void* operator new(size_t n) {
  BeforeNew();
  cpp_malloc = true;
  void* p = std::malloc(n ? n : 1);
  cpp_malloc = false;
  if (p) return p;
  throw std::bad_alloc();
}
void* operator new[](size_t n) { return ::operator new(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { ::operator delete(p); }
void operator delete(void* p, size_t) noexcept { ::operator delete(p); }
void operator delete[](void* p, size_t) noexcept { ::operator delete(p); }
void* operator new(size_t n, std::align_val_t alignment) {
  BeforeNew();
  void* p = nullptr;
  if (posix_memalign(&p, static_cast<size_t>(alignment), n ? n : 1))
    throw std::bad_alloc();
  Track(p);
  return p;
}
void* operator new[](size_t n, std::align_val_t a) {
  return ::operator new(n, a);
}
void operator delete(void* p, std::align_val_t) noexcept { std::free(p); }
void operator delete[](void* p, std::align_val_t a) noexcept {
  ::operator delete(p, a);
}
void operator delete(void* p, size_t, std::align_val_t a) noexcept {
  ::operator delete(p, a);
}
void operator delete[](void* p, size_t, std::align_val_t a) noexcept {
  ::operator delete(p, a);
}

namespace {
void Responses() {
  using namespace q4t::server::detail;
  int cases = 0;
  for (bool context_enabled : {false, true}) {
    for (auto state : {ResponseState::kUnsent, ResponseState::kHeadersStarted,
                       ResponseState::kStreaming, ResponseState::kTerminal,
                       ResponseState::kComplete}) {
      for (auto kind : {ExceptionKind::kInternal, ExceptionKind::kHostMemory}) {
        int fd[2];
        Check(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0, "socketpair");
        std::unique_ptr<HttpResponseContext> context;
        if (context_enabled) {
          context = std::make_unique<HttpResponseContext>(fd[0]);
          Check(context->SetTraceId("persistent-oom"), "trace");
          *context->State() = state;
        }
        Deny(1);
        // An independent attempted allocation proves denial is actually on.
        try {
          ::operator delete(::operator new(4096));
        } catch (const std::bad_alloc&) {
        }
        Check(denied == 1, "negative control did not deny new");
        SendExceptionResponse(fd[0], state, kind);
        Check(denied == 1, "fallback attempted C++ allocation");
        if (context) {
          SendExceptionResponse(fd[0], state, kind);
          context.reset();  // Destructor also runs while allocations fail.
          Check(denied == 1, "terminal/destructor allocated");
        }
        Allow();
        shutdown(fd[0], SHUT_WR);
        const auto wire = Read(fd[1]);
        if (state == ResponseState::kUnsent ||
            state == ResponseState::kStreaming) {
          const char* code = kind == ExceptionKind::kHostMemory
                                 ? "host_memory_exhausted"
                                 : "internal_error";
          auto pos = wire.find(code);
          Check(pos != wire.npos && wire.find(code, pos + 1) == wire.npos,
                "one error only");
          if (state == ResponseState::kUnsent) {
            Check(wire.starts_with(kind == ExceptionKind::kHostMemory
                                       ? "HTTP/1.1 503 "
                                       : "HTTP/1.1 500 "),
                  "HTTP status");
            Check(!context_enabled ||
                      wire.find("X-Request-Id: persistent-oom") != wire.npos,
                  "HTTP identity");
          } else {
            Check(wire.starts_with("data: ") &&
                      wire.ends_with("data: [DONE]\r\n\r\n"),
                  "SSE terminal");
          }
          Check(wire.find("finish_reason") == wire.npos, "false success");
        } else
          Check(wire.empty(), "appended after headers/terminal");
        close(fd[0]);
        close(fd[1]);
        ++cases;
      }
    }
  }
  for (bool stream : {false, true}) {
    int fd[2];
    Check(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0, "socketpair");
    {
      HttpResponseContext context(fd[0]);
      *context.State() =
          stream ? ResponseState::kStreaming : ResponseState::kUnsent;
      send_mode = 1;
      Deny(1);
      SendExceptionResponse(fd[0], *context.State(),
                            ExceptionKind::kHostMemory);
      send_mode = 0;
      SendExceptionResponse(fd[0], *context.State(),
                            ExceptionKind::kHostMemory);
      Check(cpp_calls == 0, "failed write fallback allocated");
      Allow();
    }
    shutdown(fd[0], SHUT_WR);
    Check(Read(fd[1]).size() == 8, "retried after partial failed response");
    close(fd[0]);
    close(fd[1]);
    ++cases;
  }
  std::printf("PASS persistent_host_response cases=%d\n", cases);
}

Calls Process(const std::vector<uint8_t>& bytes, bool video, size_t fail,
              const char* output, bool c_failure = false) {
  const std::vector<const uint8_t*> frames{bytes.data(), bytes.data()};
  const std::vector<size_t> lengths{bytes.size(), bytes.size()};
  bool threw = false;
  Calls calls;
  tracking = true;
  Deny(c_failure ? 0 : fail);
  c_calls = 0;
  c_deny_from = c_failure ? fail : 0;
  try {
    q4t::vision::ProcessorConfig cfg;
    std::string error;
    q4t::vision::ProcessedImage image;
    q4t::vision::ProcessedVideo movie;
    if (video) {
      cfg.min_pixels = 4096;
      cfg.max_pixels = 25165824;
    }
    const bool ok =
        video ? q4t::vision::ProcessVideo(frames, lengths, cfg, &movie, &error)
              : q4t::vision::ProcessImage(bytes.data(), bytes.size(), cfg,
                                          &image, &error);
    Check(ok, "fixture decode/resize failed");
    calls = {cpp_calls, c_calls};
    if (output) {
      FILE* file = std::fopen(output, "wb");
      Check(file != nullptr, "pixel output");
      const auto& pixels = video ? movie.pixel_values : image.pixel_values;
      Check(std::fwrite(pixels.data(), sizeof(float), pixels.size(), file) ==
                pixels.size(),
            "pixel write");
      std::fclose(file);
    }
  } catch (const std::bad_alloc&) {
    threw = true;
  }
  Allow();
  c_deny_from = 0;
  tracking = false;
  Check(std::all_of(live.begin(), live.end(), [](auto* p) { return !p; }),
        "processor left C/C++ allocation alive");
  Check(threw == (fail != 0), "denial did not reach expected boundary");
  return calls;
}
}  // namespace

int main(int argc, char** argv) {
  Check(argc == 4, "host_contract image_a image_b output_directory");
  Responses();
  size_t attempts = 0, c_attempts = 0;
  for (int i = 1; i <= 2; ++i) {
    std::ifstream file(argv[i], std::ios::binary);
    std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(file)), {});
    Check(!bytes.empty(), "missing source fixture");
    for (bool video : {false, true}) {
      const auto output = std::string(argv[3]) + "/pixels_" +
                          std::to_string(i) +
                          (video ? "_video.bin" : "_image.bin");
      const auto calls = Process(bytes, video, 0, output.c_str());
      Check(calls.cpp > 0 && calls.cpp <= 64 && calls.c > 0 && calls.c <= 64,
            "processor allocation bound changed");
      for (size_t fail = 1; fail <= calls.cpp; ++fail) {
        Process(bytes, video, fail, nullptr);
        ++attempts;
      }
      for (size_t fail = 1; fail <= calls.c; ++fail) {
        Process(bytes, video, fail, nullptr, true);
        ++c_attempts;
      }
      // Retry once after the fixed failure sweep, checking the full result.
      Process(bytes, video, 0, nullptr);
    }
  }
  std::printf(
      "PASS processor_owner cpp_failures=%zu c_failures=%zu successes=8\n",
      attempts, c_attempts);
}
