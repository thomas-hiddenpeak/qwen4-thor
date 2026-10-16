// Startup-only CUPTI observer. No timings, kernel replay, clock/cache controls,
// or CUDA calls inside callbacks. pCubin is a host image, NOT a GPU code VA.
#include <cupti.h>

#include <atomic>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>

// OpenSSL 3's public one-shot SHA256 ABI. Link the installed libcrypto.so.3;
// no OpenSSL development package or internal structs are required.
extern "C" unsigned char* SHA256(const unsigned char*, size_t, unsigned char*);
namespace {
CUpti_SubscriberHandle subscriber;
std::atomic<unsigned long long> count{0};
[[noreturn]] void Die(const char* message) {
  std::fprintf(stderr, "[module-load] INVALID %s\n", message);
  std::abort();
}
void Check(CUptiResult result) {
  if (result != CUPTI_SUCCESS) {
    std::fprintf(stderr, "[module-load] cupti_error=%d\n", result);
    Die("CUPTI operation failed");
  }
}
void CUPTIAPI Callback(void*, CUpti_CallbackDomain domain, CUpti_CallbackId id,
                       const void* data) {
  if (domain != CUPTI_CB_DOMAIN_RESOURCE ||
      id != CUPTI_CBID_RESOURCE_MODULE_LOADED)
    Die("unexpected callback");
  const auto* resource = static_cast<const CUpti_ResourceData*>(data);
  if (!resource || !resource->resourceDescriptor) Die("missing descriptor");
  const auto* module = static_cast<const CUpti_ModuleResourceData*>(
      resource->resourceDescriptor);
  if (!module->pCubin || !module->cubinSize ||
      module->cubinSize > 512ULL * 1024 * 1024)
    Die("missing or oversized cubin");
  unsigned char hash[32];
  if (SHA256(reinterpret_cast<const unsigned char*>(module->pCubin),
             module->cubinSize, hash) != hash)
    Die("hash failed");
  char hex[65];
  for (size_t i = 0; i < 32; ++i) std::sprintf(hex + 2 * i, "%02x", hash[i]);
  const auto sequence = count.fetch_add(1);
  if (std::fprintf(stderr,
                   "[module-load] sequence=%llu module_id=%u context=%p "
                   "bytes=%zu sha256=%s\n",
                   sequence, module->moduleId,
                   reinterpret_cast<void*>(resource->context),
                   module->cubinSize, hex) < 0)
    Die("record write failed");
}
__attribute__((constructor)) void Start() {
  const char* enabled = std::getenv("Q4T_DIAG_MODULE_OBSERVER");
  if (!enabled || std::strcmp(enabled, "1")) Die("explicit enable required");
  Check(cuptiSubscribe(&subscriber, Callback, nullptr));
  Check(cuptiEnableCallback(1, subscriber, CUPTI_CB_DOMAIN_RESOURCE,
                           CUPTI_CBID_RESOURCE_MODULE_LOADED));
  std::fprintf(stderr, "[module-load] enabled=1\n");
}
__attribute__((destructor)) void Finish() {
  Check(cuptiUnsubscribe(subscriber));
  std::fprintf(stderr, "[module-load] complete=%llu\n", count.load());
}
}  // namespace
