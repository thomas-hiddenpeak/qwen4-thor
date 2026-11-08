// Real file/kernel I/O, CPU-only doubles for embedding allocation/upload.
#include "q4t/ple/page_reader.h"
#include "q4t/ple/ple_embedding.h"
#include "q4t/ple/fp8_convert.h"
#include "q4t/server/service_health.h"
#include "q4t/server/progress_context.h"
#include "q4t/test.h"

#include <fcntl.h>
#include <unistd.h>

#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <memory>
#include <string>
#include <vector>

namespace {
using q4t::ple::PleEmbedding;
using q4t::ple::PlePageReader;
using q4t::runtime::ModelFault;
using q4t::server::ServiceHealth;
constexpr size_t kFileBytes = 64 * 1024;
constexpr size_t kRowBytes = 160;
int uploads = 0;
bool fail_event = false;

struct File {
  std::string path;
  int fd = -1;
  File() {
    std::filesystem::create_directories(Q4T_PLE_HEALTH_FIXTURES);
    path = std::string(Q4T_PLE_HEALTH_FIXTURES) + "/ple-XXXXXX";
    fd = mkstemp(path.data());
  }
  ~File() {
    if (fd >= 0) close(fd);
    unlink(path.c_str());
  }
  bool Fill() {
    std::vector<uint8_t> data(kFileBytes);
    for (size_t i = 0; i < data.size(); ++i) data[i] = (i * 7 + 13) & 255;
    return fd >= 0 && ftruncate(fd, kFileBytes) == 0 &&
           pwrite(fd, data.data(), data.size(), 0) ==
               static_cast<ssize_t>(data.size());
  }
};

struct ShortReads {
  explicit ShortReads(const char* count) {
    setenv("Q4T_PLE_FAULT_INJECT", count, 1);
  }
  ~ShortReads() { unsetenv("Q4T_PLE_FAULT_INJECT"); }
};

std::unique_ptr<PlePageReader> Reader(const File& file) {
  PlePageReader* reader = nullptr;
  const auto status = PlePageReader::Create(file.path, kRowBytes, 0, 0,
                                            kFileBytes / kRowBytes, &reader);
  if (!status.ok()) std::fprintf(stderr, "%s\n", status.message().c_str());
  return std::unique_ptr<PlePageReader>(reader);
}

std::unique_ptr<PleEmbedding> Embedding(const File& file) {
  PleEmbedding::Config config;
  config.sidecar_path = file.path;
  config.total_rows = kFileBytes / kRowBytes;
  config.capacity_tokens = 1;
  PleEmbedding* embedding = nullptr;
  const auto status = PleEmbedding::Create(config, &embedding);
  if (!status.ok()) std::fprintf(stderr, "%s\n", status.message().c_str());
  return std::unique_ptr<PleEmbedding>(embedding);
}
}  // namespace

// These definitions satisfy every CUDA reference in the linked production
// embedding; adding an unmodelled CUDA dependency causes a link failure.
extern "C" cudaError_t cudaMalloc(void** out, size_t bytes) {
  *out = std::malloc(bytes);
  return *out ? cudaSuccess : cudaErrorMemoryAllocation;
}
extern "C" cudaError_t cudaHostAlloc(void** out, size_t bytes, unsigned) {
  return cudaMalloc(out, bytes);
}
extern "C" cudaError_t cudaFree(void* pointer) {
  std::free(pointer);
  return cudaSuccess;
}
extern "C" cudaError_t cudaFreeHost(void* pointer) { return cudaFree(pointer); }
extern "C" cudaError_t cudaEventCreateWithFlags(cudaEvent_t* out, unsigned) {
  *out = reinterpret_cast<cudaEvent_t>(std::malloc(1));
  return *out ? cudaSuccess : cudaErrorMemoryAllocation;
}
extern "C" cudaError_t cudaEventDestroy(cudaEvent_t event) {
  std::free(event);
  return cudaSuccess;
}
extern "C" cudaError_t cudaEventSynchronize(cudaEvent_t) {
  return fail_event ? cudaErrorUnknown : cudaSuccess;
}
extern "C" cudaError_t cudaStreamSynchronize(cudaStream_t) {
  return cudaSuccess;
}
extern "C" cudaError_t cudaEventRecord(cudaEvent_t, cudaStream_t) {
  return cudaSuccess;
}
extern "C" cudaError_t cudaMemcpyAsync(void* out, const void* in, size_t bytes,
                                       cudaMemcpyKind, cudaStream_t) {
  ++uploads;
  std::memcpy(out, in, bytes);
  return cudaSuccess;
}
namespace q4t::ple {
cudaError_t ConvertFp8ToBf16Async(const uint8_t*, uint16_t*, size_t,
                                  cudaStream_t) {
  return cudaSuccess;  // No numerical/GPU acceptance is claimed here.
}
}  // namespace q4t::ple

Q4T_TEST(ple_health_eof_is_sticky_after_file_repair) {
  File file;
  Q4T_CHECK(file.Fill());
  auto reader = Reader(file);
  Q4T_CHECK(reader);
  Q4T_CHECK(ftruncate(file.fd, 0) == 0);
  const int64_t row = 0;
  std::vector<uint8_t> out(kRowBytes, 123);
  auto result = reader->Gather(&row, 1, out.data(), nullptr);
  Q4T_CHECK(!result.ok() && reader->HealthFault() == ModelFault::kPleRead);
  Q4T_CHECK(file.Fill());
  std::fill(out.begin(), out.end(), 123);
  result = reader->Gather(&row, 1, out.data(), nullptr);
  Q4T_CHECK(!result.ok());
  Q4T_CHECK(result.message().find("unusable after I/O failure") !=
            std::string::npos);
  Q4T_CHECK(out.front() == 123 && out.back() == 123);
  return true;
}

Q4T_TEST(ple_health_recovered_short_read_does_not_poison_model) {
  File file;
  Q4T_CHECK(file.Fill());
  auto reader = Reader(file);
  Q4T_CHECK(reader);
  const int64_t row = 0;
  std::vector<uint8_t> out(kRowBytes);
  {
    ShortReads fault("3");
    Q4T_CHECK(reader->Gather(&row, 1, out.data(), nullptr).ok());
  }
  for (size_t i = 0; i < out.size(); ++i)
    Q4T_CHECK(out[i] == ((i * 7 + 13) & 255));
  Q4T_CHECK(reader->HealthFault() == ModelFault::kNone);
  Q4T_CHECK(reader->Gather(&row, 1, out.data(), nullptr).ok());
  return true;
}

Q4T_TEST(ple_health_retry_exhaustion_is_permanent) {
  File file;
  Q4T_CHECK(file.Fill());
  auto reader = Reader(file);
  Q4T_CHECK(reader);
  const int64_t row = 0;
  std::vector<uint8_t> out(kRowBytes);
  {
    ShortReads fault("999");
    Q4T_CHECK(!reader->Gather(&row, 1, out.data(), nullptr).ok());
  }
  Q4T_CHECK(reader->HealthFault() == ModelFault::kPleRead);
  Q4T_CHECK(!reader->Gather(&row, 1, out.data(), nullptr).ok());
  return true;
}

Q4T_TEST(ple_health_request_size_error_and_padding_are_not_permanent) {
  File file;
  Q4T_CHECK(file.Fill());
  auto reader = Reader(file);
  Q4T_CHECK(reader);
  const int64_t row = -1;
  std::vector<uint8_t> out(kRowBytes, 123);
  Q4T_CHECK(!reader
                 ->Gather(&row, size_t{UINT32_MAX} / kRowBytes + 1, out.data(),
                          nullptr)
                 .ok());
  Q4T_CHECK(reader->HealthFault() == ModelFault::kNone);
  Q4T_CHECK(reader->Gather(&row, 1, out.data(), nullptr).ok());
  for (auto value : out) Q4T_CHECK(value == 0);
  Q4T_CHECK(reader->HealthFault() == ModelFault::kNone);
  return true;
}

Q4T_TEST(ple_health_incomplete_backing_table_is_permanent) {
  File file;
  Q4T_CHECK(file.Fill());
  Q4T_CHECK(ftruncate(file.fd, 1) == 0);
  auto reader = Reader(file);
  Q4T_CHECK(reader);
  const int64_t row = 0;
  std::vector<uint8_t> out(kRowBytes);
  Q4T_CHECK(!reader->Gather(&row, 1, out.data(), nullptr).ok());
  Q4T_CHECK(reader->HealthFault() == ModelFault::kPleRead);
  return true;
}

Q4T_TEST(ple_health_embedding_propagates_reader_failure_before_upload) {
  File file;
  Q4T_CHECK(file.Fill());
  auto embedding = Embedding(file);
  Q4T_CHECK(embedding);
  Q4T_CHECK(ftruncate(file.fd, 0) == 0);
  const int64_t row = 0;
  std::vector<uint16_t> out(kRowBytes);
  const int before = uploads;
  Q4T_CHECK(!embedding->GatherRows(&row, 1, out.data(), nullptr).ok());
  Q4T_CHECK(embedding->HealthFault() == ModelFault::kPleRead);
  Q4T_CHECK(uploads == before);
  ServiceHealth health;
  health.model_ready = true;
  health.model_fault = embedding->HealthFault();
  Q4T_CHECK(!health.Ready() && health.live && health.gpu_healthy);
  Q4T_CHECK(health.ReadinessJson() ==
            "{\"status\":\"unhealthy\",\"live\":true,\"ready\":false}");
  return true;
}

Q4T_TEST(ple_health_upload_completion_failure_remains_unusable) {
  File file;
  Q4T_CHECK(file.Fill());
  auto embedding = Embedding(file);
  Q4T_CHECK(embedding);
  const int64_t row = 0;
  std::vector<uint16_t> out(kRowBytes);
  Q4T_CHECK(embedding->GatherRows(&row, 1, out.data(), nullptr).ok());
  fail_event = true;
  const auto failed = embedding->GatherRows(&row, 1, out.data(), nullptr);
  fail_event = false;
  Q4T_CHECK(!failed.ok());
  Q4T_CHECK(embedding->HealthFault() == ModelFault::kPleUpload);
  const int before = uploads;
  Q4T_CHECK(!embedding->GatherRows(&row, 1, out.data(), nullptr).ok());
  Q4T_CHECK(uploads == before);
  return true;
}

Q4T_TEST(ple_progress_reaped_wave_counts_but_failure_does_not) {
  using namespace q4t::server;
  File file;
  Q4T_CHECK(file.Fill());
  auto reader = Reader(file);
  Q4T_CHECK(reader);
  RequestProgress progress;
  auto lease = progress.Acquire(ProgressNowNs() + 1000000000);
  lease.Handle().Stage(ProgressStage::kPrefill);
  ProgressContext context(lease.Handle());
  ProgressExecution execution(ProgressWork::kPrefill);
  const int64_t row = 0;
  std::vector<uint8_t> out(kRowBytes);
  {
    ShortReads short_reads("3");
    Q4T_CHECK(reader->Gather(&row, 1, out.data(), nullptr).ok());
  }
  auto value = progress.Read();
  Q4T_CHECK(value.complete && value.rows[0].completed == 1 &&
            value.rows[0].stage == ProgressStage::kPrefill &&
            value.rows[0].work_id == 1);
  Q4T_CHECK(ftruncate(file.fd, 0) == 0);
  Q4T_CHECK(!reader->Gather(&row, 1, out.data(), nullptr).ok());
  value = progress.Read();
  Q4T_CHECK(value.complete && value.rows[0].completed == 1 &&
            value.rows[0].stage == ProgressStage::kPrefill);
  return true;
}

Q4T_TEST(ple_progress_prior_request_upload_does_not_count_for_new_owner) {
  using namespace q4t::server;
  File file;
  Q4T_CHECK(file.Fill());
  auto embedding = Embedding(file);
  Q4T_CHECK(embedding);
  const int64_t row = 0;
  std::vector<uint16_t> out(kRowBytes);
  Q4T_CHECK(embedding->GatherRows(&row, 1, out.data(), nullptr).ok());
  RequestProgress progress;
  auto lease = progress.Acquire(ProgressNowNs() + 1000000000);
  ProgressContext context(lease.Handle());
  ProgressExecution execution(ProgressWork::kDecode);
  Q4T_CHECK(embedding->GatherRows(&row, 1, out.data(), nullptr).ok());
  // Only the new request's one completed read wave, not the old upload or
  // this request's newly submitted (uncompleted) upload.
  const auto value = progress.Read();
  Q4T_CHECK(value.complete && value.rows[0].completed == 1);
  return true;
}
