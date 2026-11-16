#pragma once

namespace q4t::runtime {
namespace detail {
// A short visual continuation is still prefill. Dispatch must not infer its
// numerical contract from the row count alone. Keep this host-thread local;
// another worker can decode while this worker prepares a visual suffix.
inline thread_local bool prefill_kernels = false;
}  // namespace detail

inline bool UsePrefillKernels() { return detail::prefill_kernels; }

class PrefillKernelScope {
 public:
  explicit PrefillKernelScope(bool enabled)
      : previous_(detail::prefill_kernels) {
    detail::prefill_kernels = enabled;
  }
  ~PrefillKernelScope() { detail::prefill_kernels = previous_; }
  PrefillKernelScope(const PrefillKernelScope&) = delete;
  PrefillKernelScope& operator=(const PrefillKernelScope&) = delete;

 private:
  bool previous_;
};
}  // namespace q4t::runtime
