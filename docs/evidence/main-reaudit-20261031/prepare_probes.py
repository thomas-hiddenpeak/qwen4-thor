from pathlib import Path
import json
import subprocess

ROOT = Path('/home/rm01/models/dev/qwen4-thor')
OUT = ROOT / '.q4t-work/main-reaudit-20261031'
text = (ROOT / 'tests/residency_host_contract_test.cpp').read_text()


def change(old, new):
    global text
    assert text.count(old) == 1, old
    text = text.replace(old, new)


change('enum class Split {', '''std::array<void*, 8> audit_host_blocks{};
std::array<size_t, 8> audit_host_sizes{};
std::atomic<bool> audit_worker_oom{false};
pthread_t audit_main_thread = pthread_self();

size_t AuditPaddingChanges() {
  size_t changed = 0;
  for (size_t i = 0; i < audit_host_blocks.size(); ++i) {
    if (!audit_host_blocks[i]) continue;
    const auto* bytes = static_cast<const unsigned char*>(audit_host_blocks[i]);
    for (size_t j = 0; j < 64; ++j)
      changed += bytes[audit_host_sizes[i] + j] != 0xa5;
  }
  return changed;
}

enum class Split {''')
change('explicit Fixture(Split split = Split::kNone) {',
       'explicit Fixture(Split split = Split::kNone, int malformed = 0) {')
change('const size_t bytes = region == 0 ? 4096 : region == 1 ? 512 : 4;',
'''size_t bytes = region == 0 ? 4096 : region == 1 ? 512 : 4;
            if (malformed == 1 && e == 0 && region == 0 && p == 0)
              bytes = 2048;
            if (malformed == 2 && e == 1 && region == 2 && p == 0 && scalar == 0)
              bytes = 8;''')
change('std::to_string(region == 2 ? 1 : bytes)',
       'std::to_string(region == 2 ? bytes / 4 : bytes)')
change('value.assign(reinterpret_cast<const char*>(&number), 4);',
       'std::memcpy(value.data(), &number, sizeof(number));')
change('return __real_cudaMalloc(p, n);', '''*p = std::malloc(n + 64);
  if (!*p) return cudaErrorMemoryAllocation;
  std::memset(*p, 0xa5, n + 64);
  for (size_t i = 0; i < audit_host_blocks.size(); ++i) {
    if (!audit_host_blocks[i]) {
      audit_host_blocks[i] = *p;
      audit_host_sizes[i] = n;
      return cudaSuccess;
    }
  }
  std::abort();''')
change('cudaError_t __real_cudaFreeHost(void* p) { return __real_cudaFree(p); }',
'''cudaError_t __real_cudaFreeHost(void* p) {
  for (auto& block : audit_host_blocks)
    if (block == p) block = nullptr;
  return __real_cudaFree(p);
}''')

text += r'''

#include "q4t/runtime/residency_config.h"

extern "C" void* __real__Znwm(size_t);
extern "C" void* __wrap__Znwm(size_t bytes) {
  if (audit_worker_oom.load() &&
      !pthread_equal(pthread_self(), audit_main_thread) &&
      audit_worker_oom.exchange(false)) {
    std::fprintf(stderr, "AUDIT_INJECT: worker operator new bad_alloc\n");
    throw std::bad_alloc();
  }
  return __real__Znwm(bytes);
}

// These probes PASS when a specific defect is observed. They are evidence,
// not acceptance tests asserting the implementation is correct.
Q4T_TEST(audit_short_weight_observation) {
  Environment environment;
  setenv("Q4T_MOE_INLINE_MISS_LIMIT", "2", 1);
  Fixture fixture(Split::kNone, 1);
  Owner owner;
  Q4T_CHECK(owner.Init(fixture).ok());
  int32_t needed[] = {0}, slots[1];
  const auto status = owner.residency.Resolve(needed, 1, slots, nullptr);
  Q4T_CHECK(status.ok());
  const auto* bytes = owner.residency.Layout().dn_packed_expert(slots[0]);
  std::printf("OBSERVED short_weight: status_ok=%d first=%u tail=%u expected=%u\n",
              status.ok(), bytes[0], bytes[4095], 17u);
  Q4T_CHECK(bytes[0] == 17 && bytes[4095] == 0xa5);
  owner.residency.Free();
  Q4T_CHECK(LiveResources() == 0 && live_threads == 0);
  return true;
}

Q4T_TEST(audit_oversized_scalar_observation) {
  Environment environment;
  setenv("Q4T_MOE_INLINE_MISS_LIMIT", "2", 1);
  Fixture fixture(Split::kNone, 2);
  Owner owner;
  Q4T_CHECK(owner.Init(fixture).ok());
  int32_t needed[] = {0, 1}, slots[2];
  const auto status = owner.residency.Resolve(needed, 2, slots, nullptr);
  const auto changed = AuditPaddingChanges();
  std::printf("OBSERVED oversized_scalar: status_ok=%d bytes_past_pinned=%zu\n",
              status.ok(), changed);
  Q4T_CHECK(status.ok() && changed == 4);
  owner.residency.Free();
  Q4T_CHECK(LiveResources() == 0 && live_threads == 0);
  return true;
}

Q4T_TEST(audit_hot_duplicates_observation) {
  Environment environment;
  setenv("Q4T_MOE_INLINE_MISS_LIMIT", "2", 1);
  Fixture fixture;
  Owner owner;
  q4t::runtime::ResidencyConfig config;
  const auto parsed = q4t::runtime::ParseResidencyConfig(
      R"({"0":[0,0,1]})", 1, 2, 2, &config);
  Q4T_CHECK(parsed.ok() && config.layer_slots[0] == 2);
  Q4T_CHECK(owner.Init(fixture).ok());
  const auto hot = owner.residency.InitHot(config.hot_lists[0], nullptr);
  Q4T_CHECK(hot.ok());
  int32_t needed[] = {0, 1}, slots[2];
  const auto status = owner.residency.Resolve(needed, 2, slots, nullptr);
  std::printf("OBSERVED duplicate_hot: config_ok=%d init_hot_ok=%d slots=%d,%d "
              "resolve_ok=%d reason=%s\n", parsed.ok(), hot.ok(),
              owner.residency.SlotExpert(0), owner.residency.SlotExpert(1),
              status.ok(), status.message().c_str());
  Q4T_CHECK(owner.residency.SlotExpert(0) == 0 &&
            owner.residency.SlotExpert(1) == 0 && !status.ok());
  owner.residency.Free();
  Q4T_CHECK(LiveResources() == 0 && live_threads == 0);
  return true;
}

Q4T_TEST(audit_worker_exception_observation) {
  Environment environment;
  Fixture fixture;
  Owner owner;
  Q4T_CHECK(owner.Init(fixture).ok());
  int32_t needed[] = {0, 1}, slots[2];
  audit_worker_oom.store(true);
  const auto status = owner.residency.Resolve(needed, 2, slots, nullptr);
  std::printf("WORKER_EXCEPTION_HANDLED status_ok=%d\n", status.ok());
  return false;  // Current code terminates before this line.
}
'''
(OUT / 'residency_probe.cpp').write_text(text)

parent = OUT / 'parent'
names = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only',
    'c26e97a', '--', 'include', 'src'], cwd=ROOT, text=True).splitlines()
for name in names:
    dest = parent / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(subprocess.check_output(['git', 'show', 'c26e97a:' + name],
                                             cwd=ROOT))

cmake = '''cmake_minimum_required(VERSION 3.25)
project(MainAuditProbes LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Threads REQUIRED)
set(AUDIT_SOURCE_ROOT "''' + str(ROOT) + '''" CACHE PATH "Production source snapshot")
add_executable(residency_probe residency_probe.cpp
  "''' + str(ROOT) + '''/tests/test_main.cpp"
  ${AUDIT_SOURCE_ROOT}/src/quant/moe_residency.cpp
  ${AUDIT_SOURCE_ROOT}/src/io/json.cpp
  ${AUDIT_SOURCE_ROOT}/src/io/safetensors.cpp
  ${AUDIT_SOURCE_ROOT}/src/io/weight_loader.cpp)
target_include_directories(residency_probe PRIVATE ${AUDIT_SOURCE_ROOT}/include
  /usr/local/cuda/include)
target_compile_definitions(residency_probe PRIVATE
  Q4T_IO_FIXTURE_ROOT="${CMAKE_CURRENT_BINARY_DIR}/fixtures")
target_compile_options(residency_probe PRIVATE -Wall -Wextra -Werror)
target_link_libraries(residency_probe PRIVATE Threads::Threads)
target_link_options(residency_probe PRIVATE -static-libstdc++
  -Wl,--wrap=pthread_create,--wrap=pthread_join,--wrap=_Znwm
  -Wl,--wrap=_ZnwmRKSt9nothrow_t
  -Wl,--wrap=cudaMalloc,--wrap=cudaFree
  -Wl,--wrap=cudaHostAlloc,--wrap=cudaFreeHost
  -Wl,--wrap=cudaEventCreateWithFlags,--wrap=cudaEventDestroy
  -Wl,--wrap=cudaStreamSynchronize)
'''
(OUT / 'CMakeLists.txt').write_text(cmake)
(OUT / 'probe-note.json').write_text(json.dumps(dict(
    scope='Synthetic CPU test harness with 64-byte allocation guard padding; no production edits',
    base_test='tests/residency_host_contract_test.cpp',
    observations='PASS means defect reproduced, not acceptance',
    worker_abort='run in a subprocess with core dumps disabled'), indent=2) + '\n')
print('Prepared synthetic probes and original runtime source comparison.')
