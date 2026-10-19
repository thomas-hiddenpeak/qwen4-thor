// Diagnostic-only CUPTI subscriber. Copies selected loaded cubins before the
// callback-owned pointers expire; never patches a kernel or launch argument.
#include <cupti.h>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <string_view>

namespace {
CUpti_SubscriberHandle subscriber;
size_t total_bytes = 0;
unsigned saved = 0;
[[noreturn]] void Fail(const char* message) {
  std::fprintf(stderr, "module capture FAIL: %s\n", message);
  std::_Exit(90);
}
void CUPTIAPI Observe(void*, CUpti_CallbackDomain domain, CUpti_CallbackId id,
                      const void* payload) {
  if (domain != CUPTI_CB_DOMAIN_RESOURCE ||
      id != CUPTI_CBID_RESOURCE_MODULE_LOADED)
    return;
  const auto* resource = static_cast<const CUpti_ResourceData*>(payload);
  const auto* module = static_cast<const CUpti_ModuleResourceData*>(
      resource->resourceDescriptor);
  if (!module || !module->pCubin || !module->cubinSize)
    Fail("missing module data");
  const std::string_view data(module->pCubin, module->cubinSize);
  const bool bf16 = data.find(
                        "cutlass_80_wmma_tensorop_bf16_s161616gemm_bf16_16x16_"
                        "64x1_tn_align8") != data.npos;
  const bool fp32 =
      data.find(
          "cutlass_80_wmma_tensorop_s161616gemm_bf16_16x16_64x1_tn_align8") !=
      data.npos;
  if (!bf16 && !fp32) return;
  if (module->cubinSize > 256u * 1024u * 1024u ||
      total_bytes + module->cubinSize > 512u * 1024u * 1024u || saved >= 4)
    Fail("module capture budget exceeded");
  const char* root = std::getenv("UPSTREAM_CUBIN_OUTPUT");
  if (!root || !*root) Fail("missing output directory");
  const std::string name = std::string(root) + "/module-" +
                           std::to_string(module->moduleId) + ".cubin";
  FILE* file = std::fopen(name.c_str(), "wbx");
  if (!file) Fail("module file exists or open failed");
  const size_t written =
      std::fwrite(module->pCubin, 1, module->cubinSize, file);
  const int closed = std::fclose(file);
  if (written != module->cubinSize || closed) Fail("module write failed");
  total_bytes += written;
  ++saved;
  std::fprintf(stderr, "captured module=%u bytes=%zu bf16=%d fp32=%d\n",
               module->moduleId, written, bf16, fp32);
}
__attribute__((constructor)) void Start() {
  if (!std::getenv("UPSTREAM_CUBIN_OUTPUT"))
    Fail("capture output not configured");
  if (cuptiSubscribe(&subscriber, Observe, nullptr) != CUPTI_SUCCESS)
    Fail("cuptiSubscribe");
  if (cuptiEnableCallback(1, subscriber, CUPTI_CB_DOMAIN_RESOURCE,
                          CUPTI_CBID_RESOURCE_MODULE_LOADED) != CUPTI_SUCCESS)
    Fail("cuptiEnableCallback");
}
__attribute__((destructor)) void Finish() {
  std::fprintf(stderr, "module capture completed saved=%u bytes=%zu\n", saved,
               total_bytes);
}
}  // namespace
