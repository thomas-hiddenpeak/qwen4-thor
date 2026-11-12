// Root-issued, one-use cold-start custody. Never a substitute for qualification
// or for the core durable model authorization/stop/recovery ledger.
#pragma once

#include <cstdint>
#include <string>

#include "q4t/service/device_filter.h"

namespace q4t::service {

struct DeviceAdmission {
  std::string service_id;
  std::string manifest_sha256;
  std::string boot_id;
  std::string domain_id;
  std::string qualification_sha256;
  std::string journal_receipt_sha256;
  uint64_t ticket_revision = 0;
  uint64_t issued_ns = 0;
  uint64_t expires_ns = 0;
  uint32_t runtime_uid = 0;
  uint64_t lease_device = 0;
  uint64_t lease_inode = 0;
  DeviceFilterIdentity filter;
  bool operator==(const DeviceAdmission&) const = default;
};

// Bounded exact-integer JSON for the privileged local control channel. Parsing
// claims alone conveys no authority; consumption still needs kernel objects.
Status ParseDeviceAdmission(const std::string& bytes, DeviceAdmission* output);

// The privileged deployment path must commit its bounded cold-start charge
// and qualify a complete host fence BEFORE issuance. No disk access, CUDA,
// NVML, process spawning or implicit qualification takes place here.
// The immutable memfd and one-use read-only pipe are root-owned. They are not
// sent to models or controllers. Merely copying their JSON is not authority.
Status IssueDeviceAdmission(const DeviceAdmission& admission,
                            const DeviceFilter& filter, int lease_fd,
                            const std::string& lease_path,
                            OwnedFd* receipt, OwnedFd* token);

// Check immutable origin, bounded schema and root-owned pipe identity without
// consuming. Unknown/missing credentials never create replacement credentials.
Status InspectDeviceAdmission(int receipt, int token,
                              DeviceAdmission* admission);

// The Guardian supplies its actual monotonic clock, expected deployment and
// boot identity. All binding checks happen before the one-use kernel read.
// Success permits only the cold-start preflight; a model still needs its normal
// persistent grant and an actual bounded CUDA probe when NVML is unavailable.
Status ConsumeDeviceAdmission(int receipt, int token,
                              const DeviceAdmission& expected,
                              const DeviceFilter& filter, int lease_fd,
                              const std::string& lease_path, uint64_t now_ns);

}  // namespace q4t::service
