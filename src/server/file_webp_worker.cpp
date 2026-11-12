#include "q4t/server/file_webp.h"

#include <dlfcn.h>
#include <fcntl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>
#include <webp/decode.h>
#include <zlib.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "file_webp_protocol.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/lifetime.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {
namespace {
using namespace webp_detail;
struct Runtime {
  decltype(&WebPGetDecoderVersion) version = nullptr;
  decltype(&WebPInitDecoderConfigInternal) initialize = nullptr;
  decltype(&WebPGetFeaturesInternal) features = nullptr;
  decltype(&WebPDecode) decode = nullptr;
  decltype(&WebPFreeDecBuffer) release = nullptr;
  decltype(&zlibVersion) zversion = nullptr;
  decltype(&compressBound) bound = nullptr;
  decltype(&compress2) compress = nullptr;
  decltype(&crc32) crc = nullptr;
};
void* Library(const char* path, char (&sha)[65], int* retained_fd) {
  const int fd = open(path, O_RDONLY | O_CLOEXEC);
  if (fd < 0) return nullptr;
  bool retain = false;
  ScopeExit close_fd([&] {
    if (!retain) close(fd);
  });
  struct stat info {};
  if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != 0 ||
      (info.st_mode & (S_IWGRP | S_IWOTH)) || info.st_size <= 0 ||
      info.st_size > (128LL << 20))
    return nullptr;
  // Hash and load the same pinned inode. No search path or request-supplied
  // filename participates; the descriptors close before sandbox entry.
  const auto pinned = "/proc/self/fd/" + std::to_string(fd);
  trace::Digest hash;
  if (!trace::HashFile(pinned, &hash)) return nullptr;
  void* result = dlopen(pinned.c_str(), RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
  if (!result) return nullptr;
  const auto text = trace::HexDigest(hash);
  std::memcpy(sha, text.c_str(), text.size() + 1);
  *retained_fd = fd;
  retain = true;
  return result;
}
bool Load(Runtime* api, Result* result, bool missing) {
  // Keep distinct /proc/self/fd names until both loads finish. Reusing a
  // closed descriptor's path would let dlopen return its previous handle.
  int webp_fd = -1, zlib_fd = -1;
  ScopeExit close_fds([&] {
    if (webp_fd >= 0) close(webp_fd);
    if (zlib_fd >= 0) close(zlib_fd);
  });
  void* webp =
      Library(missing ? "/q4t-no-such-webp-codec" : Q4T_SYSTEM_WEBP_LIBRARY,
              result->decoder_sha256, &webp_fd);
  void* zlib =
      Library(Q4T_SYSTEM_ZLIB_LIBRARY, result->encoder_sha256, &zlib_fd);
  if (!webp || !zlib) return false;
#define Q4T_WEBP_LOAD(handle, field, symbol)                                   \
  api->field = reinterpret_cast<decltype(api->field)>(dlsym(handle, #symbol)); \
  if (!api->field) return false
  Q4T_WEBP_LOAD(webp, version, WebPGetDecoderVersion);
  Q4T_WEBP_LOAD(webp, initialize, WebPInitDecoderConfigInternal);
  Q4T_WEBP_LOAD(webp, features, WebPGetFeaturesInternal);
  Q4T_WEBP_LOAD(webp, decode, WebPDecode);
  Q4T_WEBP_LOAD(webp, release, WebPFreeDecBuffer);
  Q4T_WEBP_LOAD(zlib, zversion, zlibVersion);
  Q4T_WEBP_LOAD(zlib, bound, compressBound);
  Q4T_WEBP_LOAD(zlib, compress, compress2);
  Q4T_WEBP_LOAD(zlib, crc, crc32);
#undef Q4T_WEBP_LOAD
  const int version = api->version();
  if (version <= 0) return false;
  std::snprintf(result->decoder_version, sizeof(result->decoder_version),
                "%d.%d.%d", (version >> 16) & 255, (version >> 8) & 255,
                version & 255);
  const char* encoder = api->zversion();
  if (!encoder || strnlen(encoder, 64) >= 64) return false;
  std::memcpy(result->encoder_version, encoder, std::strlen(encoder) + 1);
  return true;
}
void Big32(uint32_t value, std::string* bytes) {
  for (int shift : {24, 16, 8, 0}) bytes->push_back(char(value >> shift));
}
void Chunk(const Runtime& api, std::string_view type, std::string_view value,
           std::string* png) {
  Big32(static_cast<uint32_t>(value.size()), png);
  const size_t start = png->size();
  png->append(type);
  png->append(value);
  Big32(static_cast<uint32_t>(
            api.crc(0, reinterpret_cast<const Bytef*>(png->data() + start),
                    static_cast<uInt>(type.size() + value.size()))),
        png);
}
Failure Convert(const Runtime& api, const Request& request,
                const std::string& source, std::string* png) {
  WebpHeader header;
  if (!ReadWebpHeader(source, &header).ok()) return Failure::kInvalid;
  if (header.width > request.edge || header.height > request.edge ||
      uint64_t{header.width} * header.height > request.pixels)
    return Failure::kCapacity;
  const auto* bytes = reinterpret_cast<const uint8_t*>(source.data());
  WebPDecoderConfig config;
  if (!api.initialize(&config, WEBP_DECODER_ABI_VERSION))
    return Failure::kDependency;
  ScopeExit release([&] { api.release(&config.output); });
  auto status = api.features(bytes, source.size(), &config.input,
                             WEBP_DECODER_ABI_VERSION);
  if (status == VP8_STATUS_OUT_OF_MEMORY) return Failure::kCapacity;
  if (status != VP8_STATUS_OK || config.input.has_animation ||
      config.input.width != static_cast<int>(header.width) ||
      config.input.height != static_cast<int>(header.height))
    return Failure::kInvalid;
  const size_t stride = size_t{header.width} * 3;
  std::vector<uint8_t> rgb(stride * header.height);
  config.output.colorspace = MODE_RGB;
  config.output.is_external_memory = 1;
  config.output.u.RGBA = {rgb.data(), static_cast<int>(stride), rgb.size()};
  config.options.use_threads = 0;
  status = api.decode(bytes, source.size(), &config);
  if (status == VP8_STATUS_OUT_OF_MEMORY) return Failure::kCapacity;
  if (status != VP8_STATUS_OK || config.output.width != config.input.width ||
      config.output.height != config.input.height)
    return Failure::kInvalid;
  // PNG keeps decoded RGB exactly. No resize, color-profile application,
  // alpha compositing, executable metadata or original source mutation.
  std::string scanlines((stride + 1) * header.height, '\0');
  for (size_t row = 0; row < header.height; ++row)
    std::memcpy(scanlines.data() + row * (stride + 1) + 1,
                rgb.data() + row * stride, stride);
  uLongf compressed_bytes = std::min<uint64_t>(api.bound(scanlines.size()),
                                               request.output_bytes - 57);
  std::string compressed(compressed_bytes, '\0');
  const int encoded = api.compress(
      reinterpret_cast<Bytef*>(compressed.data()), &compressed_bytes,
      reinterpret_cast<const Bytef*>(scanlines.data()), scanlines.size(), 6);
  if (encoded == Z_BUF_ERROR || encoded == Z_MEM_ERROR)
    return Failure::kCapacity;
  if (encoded != Z_OK || compressed_bytes > compressed.size())
    return Failure::kInvalid;
  compressed.resize(compressed_bytes);
  *png = "\x89PNG\r\n\x1a\n";
  std::string ihdr;
  Big32(header.width, &ihdr);
  Big32(header.height, &ihdr);
  ihdr.append("\x08\x02\x00\x00\x00", 5);
  Chunk(api, "IHDR", ihdr, png);
  Chunk(api, "IDAT", compressed, png);
  Chunk(api, "IEND", {}, png);
  return png->size() <= request.output_bytes ? Failure::kNone
                                             : Failure::kCapacity;
}
int Run(int argc, char** argv, bool missing) {
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const rlimit cpu{30, 30};
  if (setrlimit(RLIMIT_CPU, &cpu)) return 2;
  Result result;
  std::string png;
  bool ready_sent = false;
  try {
    Request request;
    if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
        request.magic != kMagic || !request.bytes ||
        request.bytes > kFileInputBytes || !request.pixels ||
        request.pixels > (256ULL << 20) || !request.edge ||
        request.edge > 65536 || request.output_bytes < 1024 ||
        request.output_bytes > (256ULL << 20))
      return 3;
    std::string source(request.bytes, '\0');
    if (!detail::WorkerTransfer(source.data(), source.size(), false)) return 3;
    Runtime api;
    Ready ready = Ready::kReady;
    if (!Load(&api, &result, missing)) {
      ready = Ready::kDependency;
    } else if (!detail::EnterNativeDocumentSandbox()) {
      ready = Ready::kSandbox;
    }
    if (!detail::WorkerTransfer(&ready, sizeof(ready), true)) return 3;
    ready_sent = true;
    if (ready != Ready::kReady) return 0;
    result.failure = Convert(api, request, source, &png);
  } catch (const std::bad_alloc&) {
    result.failure = Failure::kCapacity;
  } catch (...) {
    result.failure = Failure::kInvalid;
  }
  if (!ready_sent) {
    auto ready = result.failure == Failure::kCapacity ? Ready::kCapacity
                                                      : Ready::kInvalid;
    return detail::WorkerTransfer(&ready, sizeof(ready), true) ? 0 : 3;
  }
  if (result.failure == Failure::kNone) result.bytes = png.size();
  if (!detail::WorkerTransfer(&result, sizeof(result), true)) return 3;
  if (result.bytes && !detail::WorkerTransfer(png.data(), png.size(), true))
    return 3;
  return 0;
}
}  // namespace
int MaybeRunFileWebpWorker(int argc, char** argv) {
  if (argc < 2) return -1;
  bool missing = false;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  missing = !std::strcmp(argv[1], "--file-webp-missing-worker");
#endif
  if (std::strcmp(argv[1], "--file-webp-worker") && !missing) return -1;
  try {
    return Run(argc, argv, missing);
  } catch (...) {
    return 3;
  }
}
}  // namespace q4t::server
