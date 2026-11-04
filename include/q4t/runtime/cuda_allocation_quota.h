// Process-lived requested-byte guard for project-linked CUDA calls.
#pragma once

#include <cstdint>

#include "q4t/runtime/allocation_quota.h"
#include "q4t/status.h"

namespace q4t::runtime {

enum class CudaQuotaRejection { kNone, kBytes, kEntries, kStreams, kContract };

struct CudaQuotaSnapshot {
  quota::Summary memory;
  bool configured = false;
  bool enabled = false;
  uint64_t byte_rejections = 0;
  uint64_t entry_rejections = 0;
  uint64_t stream_rejections = 0;
  uint64_t contract_failures = 0;
  uint64_t backend_failures = 0;
  size_t stream_entries = 0;
  size_t stream_capacity = 256;
};

// Configure once, before application CUDA allocations. 0 explicitly disables.
// The ledger survives ChatServer destruction: process caches remain charged.
// This does not reserve physical RAM or intercept calls inside external DSOs.
Status ConfigureCudaAllocationQuota(uint64_t limit_bytes);
CudaQuotaSnapshot ReadCudaAllocationQuota() noexcept;
CudaQuotaRejection LastCudaQuotaRejection() noexcept;
void ClearCudaQuotaRejection() noexcept;
void ReportCudaAllocationQuota(const char* event) noexcept;

class CudaQuotaReportOnDestruction {
 public:
  ~CudaQuotaReportOnDestruction() noexcept {
    ReportCudaAllocationQuota("server_destroyed");
  }
};

}  // namespace q4t::runtime
