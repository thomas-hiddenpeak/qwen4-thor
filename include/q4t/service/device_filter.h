// Optional Linux device access enforcement. No CUDA/NVML or libbpf dependency.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "q4t/service/guardian_process.h"

namespace q4t::service {

struct DeviceNumber {
  uint32_t major = 0;
  uint32_t minor = 0;
  bool all_minors = false;
  bool operator==(const DeviceNumber&) const = default;
};

struct DeviceFilterIdentity {
  uint32_t program_id = 0;
  uint32_t link_id = 0;
  uint64_t target_cgroup_id = 0;
  uint64_t allowed_cgroup_id = 0;
  std::string program_tag;
  bool operator==(const DeviceFilterIdentity&) const = default;
};

// An immutable filter denies every access to the specified character devices
// (or entire character major when all_minors is true and minor is zero)
// outside one exact cgroup. Other devices are unchanged. A cgroup created
// beneath the allowed group is NOT implicitly allowed. The deployment must
// restrict membership changes separately and establish the initial absence of
// foreign GPU contexts/handles. This hook does not revoke existing open FDs.
//
// Holding the link keeps the filter attached. Durable deployment must retain
// custody beyond controller/Guardian replacement before granting cold starts.
// Destruction detaches only this link. It does not kill/move any process, touch
// devices, or remove another program. Attachment requires host privileges.
class DeviceFilter {
 public:
  Status Attach(int target_cgroup, int allowed_cgroup,
                const std::vector<DeviceNumber>& devices);
  // Root-owned bpffs custody is explicit and outlives this process. Adoption
  // independently loads the expected immutable rule without attaching it and
  // compares kernel program tags; a JSON identity alone is not authority.
  Status Pin(const std::string& path) const;
  Status AdoptPinned(const std::string& path, int target_cgroup,
                     int allowed_cgroup,
                     const std::vector<DeviceNumber>& devices,
                     const DeviceFilterIdentity& expected);
  // For a nonprivileged Guardian after checking a root-issued admission.
  // Takes private duplicates and never trusts a bare numeric FD as identity.
  Status AdoptInherited(int link, int program,
                        const DeviceFilterIdentity& expected);
  Status RemovePin(const std::string& path) const;
  Status Verify() const;
  const DeviceFilterIdentity& Identity() const { return identity_; }
  int LinkFd() const { return link_.Get(); }
  int ProgramFd() const { return program_.Get(); }

 private:
  OwnedFd program_;
  OwnedFd link_;
  DeviceFilterIdentity identity_;
};

// Reads kernel identity from an already opened cgroup v2 directory. An
// ordinary directory or a cgroup v1 mount is never accepted as a device domain.
Status ReadCgroupId(int fd, uint64_t* id);

}  // namespace q4t::service
