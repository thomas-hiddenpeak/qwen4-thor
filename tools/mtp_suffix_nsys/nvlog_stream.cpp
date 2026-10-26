// Bounded NVLOG FIFO encoder; optional explicitly recorded ACK projection.
// Default encoding is lossless. No CUDA or profiler interfaces.
#define ZSTD_STATIC_LINKING_ONLY
#include <zstd.h>

#include <fcntl.h>
#include <poll.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

// This machine has the OpenSSL 3 runtime, but not the development headers.
// Only these stable opaque EVP C ABI functions are used; libcrypto is pinned.
extern "C" {
struct evp_md_ctx_st;
struct evp_md_st;
struct engine_st;
evp_md_ctx_st* EVP_MD_CTX_new();
void EVP_MD_CTX_free(evp_md_ctx_st*);
const evp_md_st* EVP_sha256();
int EVP_DigestInit_ex(evp_md_ctx_st*, const evp_md_st*, engine_st*);
int EVP_DigestUpdate(evp_md_ctx_st*, const void*, size_t);
int EVP_DigestFinal_ex(evp_md_ctx_st*, unsigned char*, unsigned int*);
}

namespace {
constexpr uint64_t kLogicalLimit = 8ULL << 30;
constexpr uint64_t kCompressedLimit = 256ULL << 20;
constexpr size_t kArenaBytes = 12U << 20;
constexpr size_t kBufferBytes = 128U << 10;
constexpr size_t kLineBytes = 1U << 20;
constexpr rlim_t kAddressLimit = 64U << 20;
alignas(8) std::array<unsigned char, kArenaBytes> arena;
std::array<unsigned char, kBufferBytes> input;
std::array<unsigned char, kBufferBytes> output;
std::array<unsigned char, kLineBytes> line_buffer;
using Clock = std::chrono::steady_clock;

void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

void CheckZstd(size_t result) {
  if (ZSTD_isError(result)) throw std::runtime_error(ZSTD_getErrorName(result));
}

struct Fd {
  int value = -1;
  Fd() = default;
  explicit Fd(int fd) : value(fd) {}
  Fd(const Fd&) = delete;
  Fd& operator=(const Fd&) = delete;
  ~Fd() { Close(); }
  void Close() {
    if (value >= 0) close(value);
    value = -1;
  }
};

struct Digest {
  evp_md_ctx_st* context = EVP_MD_CTX_new();
  Digest() {
    Require(context && EVP_DigestInit_ex(context, EVP_sha256(), nullptr) == 1,
            "EVP initialization failed");
  }
  ~Digest() { EVP_MD_CTX_free(context); }
  void Add(const void* data, size_t size) {
    Require(EVP_DigestUpdate(context, data, size) == 1, "EVP update failed");
  }
  std::string Finish() {
    std::array<unsigned char, 32> bytes{};
    unsigned int size = 0;
    Require(EVP_DigestFinal_ex(context, bytes.data(), &size) == 1 && size == 32,
            "EVP finalization failed");
    std::string value;
    for (unsigned char byte : bytes) {
      char hex[3];
      std::snprintf(hex, sizeof(hex), "%02x", byte);
      value += hex;
    }
    return value;
  }
};

std::string Quote(const std::string& text) {
  std::string result = "\"";
  for (unsigned char c : text) {
    if (c == '\\' || c == '"') {
      result += '\\';
      result += static_cast<char>(c);
    } else if (c < 32) {
      char escaped[7];
      std::snprintf(escaped, sizeof(escaped), "\\u%04x", c);
      result += escaped;
    } else {
      result += static_cast<char>(c);
    }
  }
  return result + '"';
}

void WriteAll(int fd, const void* source, size_t size) {
  const auto* data = static_cast<const unsigned char*>(source);
  while (size) {
    const ssize_t written = write(fd, data, size);
    if (written < 0 && errno == EINTR) continue;
    Require(written > 0, "output write failed");
    data += written;
    size -= static_cast<size_t>(written);
  }
}

struct stat Pinned(int fd, const std::string& path, bool fifo) {
  struct stat opened{}, named{};
  Require(fstat(fd, &opened) == 0 && lstat(path.c_str(), &named) == 0,
          "source/output stat failed");
  Require(opened.st_dev == named.st_dev && opened.st_ino == named.st_ino &&
              (fifo ? S_ISFIFO(named.st_mode) : S_ISREG(named.st_mode)),
          "source/output inode or type changed");
  return opened;
}

void JsonFile(const std::string& path, const std::string& text) {
  const std::string temporary = path + ".tmp";
  Fd fd(open(temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW |
                               O_CLOEXEC, 0600));
  Require(fd.value >= 0, "exclusive receipt creation failed");
  WriteAll(fd.value, text.data(), text.size());
  Require(fsync(fd.value) == 0, "receipt fsync failed");
  Pinned(fd.value, temporary, false);
  Require(link(temporary.c_str(), path.c_str()) == 0,
          "receipt publish would overwrite or failed");
  Pinned(fd.value, path, false);
  Require(unlink(temporary.c_str()) == 0, "receipt temporary unlink failed");
}

uint64_t Number(const std::string& value) {
  Require(!value.empty() && value.find_first_not_of("0123456789") ==
                                std::string::npos, "invalid integer argument");
  return std::stoull(value);
}

bool Take(std::string_view& text, std::string_view literal) {
  if (!text.starts_with(literal)) return false;
  text.remove_prefix(literal.size());
  return true;
}

bool Spaces(std::string_view& text) {
  const size_t count = text.find_first_not_of(' ');
  if (count == 0 || count == std::string_view::npos) return false;
  text.remove_prefix(count);
  return true;
}

bool Unsigned(std::string_view& text, int base = 10) {
  uint64_t value = 0;
  const auto result = std::from_chars(text.data(), text.data() + text.size(),
                                      value, base);
  if (result.ec != std::errc{} || result.ptr == text.data()) return false;
  text.remove_prefix(static_cast<size_t>(result.ptr - text.data()));
  return true;
}

bool SerializationAck(std::string_view text) {
  // A deliberately narrow no-payload INFO record from the pinned tool. The
  // terminal parser ignores this form. Unknown/changed forms are preserved.
  if (text.size() < 14 || text.size() > 1024 || text.front() != 'I') return false;
  for (size_t i = 1; i <= 12; ++i) {
    if (i == 3 || i == 6) {
      if (text[i] != ':') return false;
    } else if (i == 9) {
      if (text[i] != '.') return false;
    } else if (text[i] < '0' || text[i] > '9') {
      return false;
    }
  }
  if (text.substr(1, 2) > "23" || text.substr(4, 2) > "59" ||
      text.substr(7, 2) > "59") return false;
  text.remove_prefix(13);
  return Take(text, "| quadd_verbose_daemon_evtsrc_trace_events |") &&
         Spaces(text) && Take(text, "nsys pid=") && Unsigned(text) &&
         Take(text, " tid=") && Spaces(text) && Unsigned(text) &&
         Take(text, " | Trace.cpp:2070 [SerializeEventImpl]: Trace[0x") &&
         Unsigned(text, 16) &&
         Take(text, "]: Serialized event with timestamp {") && Unsigned(text) &&
         Take(text, ", ") && Unsigned(text) && Take(text, "}\n") && text.empty();
}

struct Options {
  std::string fifo, compressed, ready, receipt;
  uint64_t device = 0, inode = 0, logical = kLogicalLimit;
  uint64_t compressed_limit = kCompressedLimit, seconds = 900;
  int control = -1;
  bool project = false;
};

Options Parse(int argc, char** argv) {
  Options options;
  Require(argc == 21 || argc == 23, "expected ten pairs and optional projection");
  std::string seen;
  for (int i = 1; i < argc; i += 2) {
    const std::string name = argv[i], value = argv[i + 1];
    Require(seen.find("|" + name + "|") == std::string::npos,
            "duplicate option");
    seen += "|" + name + "|";
    if (name == "--fifo") options.fifo = value;
    else if (name == "--output") options.compressed = value;
    else if (name == "--ready") options.ready = value;
    else if (name == "--receipt") options.receipt = value;
    else if (name == "--fifo-device") options.device = Number(value);
    else if (name == "--fifo-inode") options.inode = Number(value);
    else if (name == "--control-fd") {
      const uint64_t fd = Number(value);
      Require(fd >= 3 && fd < 1048576, "invalid control fd");
      options.control = static_cast<int>(fd);
    } else if (name == "--logical-limit") options.logical = Number(value);
    else if (name == "--compressed-limit") {
      options.compressed_limit = Number(value);
    } else if (name == "--timeout-seconds") options.seconds = Number(value);
    else if (name == "--projection") {
      Require(value == "drop_serialized_ack_v1", "unknown projection");
      options.project = true;
    } else throw std::runtime_error("unknown option");
  }
  Require(!options.fifo.empty() && !options.compressed.empty() &&
              !options.ready.empty() && !options.receipt.empty() &&
              options.control >= 0 && options.inode > 0 &&
              options.logical <= kLogicalLimit && options.logical > 0 &&
              options.compressed_limit <= kCompressedLimit &&
              options.compressed_limit > 0 && options.seconds > 0 &&
              options.seconds <= 3600, "invalid frozen options");
  return options;
}

int Encode(const Options& options) {
  const auto begin = Clock::now();
  const auto deadline = begin + std::chrono::seconds(options.seconds);
  uint64_t logical_bytes = 0, compressed_bytes = 0;
  uint64_t input_bytes = 0, input_lines = 0, retained_lines = 0;
  uint64_t discarded_bytes = 0, discarded_lines = 0;
  size_t line_size = 0;
  bool finished = false, eof = false, frame = false, synced = false;
  std::string failure, logical_sha, compressed_sha;
  std::string input_sha, discarded_sha;
  struct stat output_identity{};
  int pipe_bytes = 0;
  try {
    rlimit limit{kAddressLimit, kAddressLimit};
    Require(setrlimit(RLIMIT_AS, &limit) == 0, "64MiB address-space cap failed");
    Digest raw_hash, compressed_hash, input_hash, discarded_hash;
    Require(ZSTD_versionNumber() == ZSTD_VERSION_NUMBER,
            "zstd header/library version mismatch");
    ZSTD_CCtx* context = ZSTD_initStaticCCtx(arena.data(), arena.size());
    Require(context, "static compression arena too small");
    for (auto [parameter, value] : {
             std::pair{ZSTD_c_compressionLevel, 1},
             std::pair{ZSTD_c_windowLog, 20},
             std::pair{ZSTD_c_nbWorkers, 0},
             std::pair{ZSTD_c_checksumFlag, 1},
             std::pair{ZSTD_c_contentSizeFlag, 0},
             std::pair{ZSTD_c_dictIDFlag, 0},
             std::pair{ZSTD_c_enableLongDistanceMatching, 0}}) {
      CheckZstd(ZSTD_CCtx_setParameter(context, parameter, value));
    }
    Fd fifo(open(options.fifo.c_str(), O_RDONLY | O_NONBLOCK | O_NOFOLLOW |
                                          O_CLOEXEC));
    Require(fifo.value >= 0, "FIFO reader open failed");
    const auto identity = Pinned(fifo.value, options.fifo, true);
    Require(static_cast<uint64_t>(identity.st_dev) == options.device &&
                static_cast<uint64_t>(identity.st_ino) == options.inode,
            "FIFO differs from controller identity");
    Fd keeper(open(options.fifo.c_str(), O_WRONLY | O_NONBLOCK | O_NOFOLLOW |
                                            O_CLOEXEC));
    Require(keeper.value >= 0, "FIFO keeper open failed");
    Pinned(keeper.value, options.fifo, true);
    pipe_bytes = fcntl(fifo.value, F_GETPIPE_SZ);
    Require(pipe_bytes > 0, "FIFO capacity unavailable");
    Fd destination(open(options.compressed.c_str(), O_WRONLY | O_CREAT |
        O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600));
    Require(destination.value >= 0, "exclusive compressed output failed");
    output_identity = Pinned(destination.value, options.compressed, false);
    JsonFile(options.ready, "{\"schema\":1,\"ready\":true,\"pid\":" +
        std::to_string(getpid()) + ",\"fifo_device\":" +
        std::to_string(options.device) + ",\"fifo_inode\":" +
        std::to_string(options.inode) + "}\n");
    const auto emit = [&](size_t count) {
      Require(count <= options.compressed_limit - compressed_bytes,
              "compressed byte cap exceeded");
      WriteAll(destination.value, output.data(), count);
      compressed_hash.Add(output.data(), count);
      compressed_bytes += count;
      Pinned(destination.value, options.compressed, false);
    };
    const auto encode = [&](const unsigned char* data, size_t size) {
      logical_bytes += size;
      raw_hash.Add(data, size);
      ZSTD_inBuffer in{data, size, 0};
      while (in.pos < in.size) {
        ZSTD_outBuffer out{output.data(), output.size(), 0};
        CheckZstd(ZSTD_compressStream2(context, &out, &in, ZSTD_e_continue));
        emit(out.pos);
      }
    };
    while (!eof) {
      Require(Clock::now() < deadline, "encoder deadline exhausted");
      Pinned(fifo.value, options.fifo, true);
      pollfd descriptors[2]{{fifo.value, POLLIN, 0},
                            {finished ? -1 : options.control, POLLIN, 0}};
      const int count = poll(descriptors, 2, 50);
      if (count < 0 && errno == EINTR) continue;
      Require(count >= 0, "FIFO/control poll failed");
      if (!finished && descriptors[1].revents) {
        char command[2];
        const ssize_t size = read(options.control, command, sizeof(command));
        Require(size == 1 && command[0] == 'F',
                "control closed or invalid; no successful finish");
        finished = true;
        keeper.Close();
      }
      if (descriptors[0].revents & (POLLERR | POLLNVAL)) {
        throw std::runtime_error("FIFO poll error");
      }
      if (!(descriptors[0].revents & (POLLIN | POLLHUP))) continue;
      const ssize_t size = read(fifo.value, input.data(), input.size());
      if (size < 0 && (errno == EAGAIN || errno == EINTR)) continue;
      Require(size >= 0, "FIFO read failed");
      if (size == 0) {
        Require(finished, "FIFO EOF before controller finish");
        eof = true;
        break;
      }
      Require(static_cast<uint64_t>(size) <= options.logical - input_bytes,
              "logical byte cap exceeded");
      input_bytes += static_cast<uint64_t>(size);
      input_hash.Add(input.data(), static_cast<size_t>(size));
      if (!options.project) {
        encode(input.data(), static_cast<size_t>(size));
        continue;
      }
      size_t offset = 0;
      while (offset < static_cast<size_t>(size)) {
        const auto* newline = static_cast<const unsigned char*>(
            std::memchr(input.data() + offset, '\n', size - offset));
        const size_t count = newline ? newline - input.data() - offset + 1
                                     : size - offset;
        Require(count <= kLineBytes - line_size, "projection line cap exceeded");
        std::memcpy(line_buffer.data() + line_size, input.data() + offset, count);
        line_size += count;
        offset += count;
        if (!newline) continue;
        ++input_lines;
        if (SerializationAck(std::string_view(
                reinterpret_cast<const char*>(line_buffer.data()), line_size))) {
          discarded_bytes += line_size;
          ++discarded_lines;
          discarded_hash.Add(line_buffer.data(), line_size);
        } else {
          ++retained_lines;
          encode(line_buffer.data(), line_size);
        }
        line_size = 0;
      }
    }
    Require(!options.project || line_size == 0,
            "projection input ends in an incomplete line");
    size_t remaining = 1;
    while (remaining) {
      Require(Clock::now() < deadline, "frame finish deadline exhausted");
      ZSTD_inBuffer in{nullptr, 0, 0};
      ZSTD_outBuffer out{output.data(), output.size(), 0};
      remaining = ZSTD_compressStream2(context, &out, &in, ZSTD_e_end);
      CheckZstd(remaining);
      emit(out.pos);
    }
    frame = true;
    Require(fsync(destination.value) == 0 &&
                fchmod(destination.value, 0444) == 0 &&
                fsync(destination.value) == 0, "compressed fsync/chmod failed");
    synced = true;
    Pinned(fifo.value, options.fifo, true);
    Pinned(destination.value, options.compressed, false);
    logical_sha = raw_hash.Finish();
    compressed_sha = compressed_hash.Finish();
    input_sha = input_hash.Finish();
    discarded_sha = discarded_hash.Finish();
  } catch (const std::exception& error) {
    failure = error.what();
  }
  rusage usage{};
  Require(getrusage(RUSAGE_SELF, &usage) == 0, "worker rusage failed");
  const double elapsed = std::chrono::duration<double>(Clock::now()-begin).count();
  const bool valid = failure.empty() && finished && eof && frame && synced;
  std::string receipt = "{\"schema\":1,\"status\":" +
      Quote(valid ? "VALID" : "INVALID") + ",\"error\":" + Quote(failure) +
      ",\"pid\":" + std::to_string(getpid()) + ",\"logical_bytes\":" +
      std::to_string(logical_bytes) + ",\"logical_sha256\":" + Quote(logical_sha) +
      ",\"compressed_bytes\":" + std::to_string(compressed_bytes) +
      ",\"compressed_sha256\":" + Quote(compressed_sha) +
      ",\"output_device\":" + std::to_string(output_identity.st_dev) +
      ",\"output_inode\":" + std::to_string(output_identity.st_ino) +
      ",\"logical_limit\":" + std::to_string(options.logical) +
      ",\"compressed_limit\":" + std::to_string(options.compressed_limit) +
      ",\"controlled_workspace_bytes\":" +
      std::to_string(kArenaBytes + 2*kBufferBytes + kLineBytes) +
      ",\"address_space_limit_bytes\":" + std::to_string(kAddressLimit) +
      ",\"fifo_kernel_capacity_bytes\":" + std::to_string(pipe_bytes) +
      ",\"maxrss_kib\":" + std::to_string(usage.ru_maxrss) +
      ",\"elapsed_seconds\":" + std::to_string(elapsed) +
      ",\"zstd_version\":" + std::to_string(ZSTD_VERSION_NUMBER) +
      ",\"level\":1,\"window_log\":20,\"workers\":0,\"checksum\":true" +
      ",\"projection\":" +
      Quote(options.project ? "drop_serialized_ack_v1" : "none") +
      ",\"input_bytes\":" + std::to_string(input_bytes) +
      ",\"input_sha256\":" + Quote(input_sha) +
      ",\"input_lines\":" + std::to_string(input_lines) +
      ",\"retained_lines\":" + std::to_string(retained_lines) +
      ",\"discarded_bytes\":" + std::to_string(discarded_bytes) +
      ",\"discarded_lines\":" + std::to_string(discarded_lines) +
      ",\"discarded_sha256\":" + Quote(discarded_sha) +
      ",\"input_eof\":" + (eof ? "true" : "false") +
      ",\"frame_completed\":" + (frame ? "true" : "false") +
      ",\"fsynced\":" + (synced ? "true" : "false") + "}\n";
  JsonFile(options.receipt, receipt);
  if (!valid) std::fprintf(stderr, "nvlog stream: %s\n", failure.c_str());
  return valid ? 0 : 1;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    return Encode(Parse(argc, argv));
  } catch (const std::exception& error) {
    std::fprintf(stderr, "nvlog stream startup/receipt: %s\n", error.what());
    return 1;
  }
}
