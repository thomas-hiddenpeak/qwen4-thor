// CPU contracts against the actual StartupPlan and WeightLoader archives.
#include <fcntl.h>
#include <sys/types.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <memory>
#include <string>
#include <vector>

#include "q4t/runtime/startup_plan.h"

namespace {
int cases = 0;
int cache_advice_calls = 0;

void Require(bool condition, const std::string& message) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", message.c_str());
    std::exit(1);
  }
}

void Pass(const char* name) {
  ++cases;
  std::printf("PASS: %s\n", name);
}

struct Snapshot {
  size_t bytes;
  std::vector<q4t::runtime::StartupAllocation> entries;
};

Snapshot Save(const q4t::runtime::StartupPlan& plan) {
  return {plan.device_bytes(), plan.entries()};
}

void Unchanged(const q4t::runtime::StartupPlan& plan, const Snapshot& before) {
  Require(plan.device_bytes() == before.bytes, "failed call changed total");
  Require(plan.entries().size() == before.entries.size(),
          "failed call changed entry count");
  for (size_t i = 0; i < before.entries.size(); ++i) {
    Require(plan.entries()[i].name == before.entries[i].name &&
                plan.entries()[i].bytes == before.entries[i].bytes,
            "failed call changed an existing entry");
  }
}

template <class Operation>
void Rejected(const char* name, q4t::runtime::StartupPlan& plan,
              Operation operation) {
  const Snapshot before = Save(plan);
  const auto result = operation();
  Require(!result.ok() && !result.message().empty(),
          std::string(name) + " unexpectedly succeeded");
  Unchanged(plan, before);
  Pass(name);
}

void TestArithmetic() {
  using q4t::runtime::StartupPlan;
  StartupPlan plan;
  Require(plan.Add("matrix", {2, 3}, 2).ok(), "valid matrix rejected");
  Require(plan.device_bytes() == 12 && plan.entries().size() == 1 &&
              plan.entries()[0].name == "matrix" &&
              plan.entries()[0].bytes == 12,
          "matrix result mismatch");
  Pass("add_matrix");
  Require(plan.Add("scalar", {}, 4).ok(), "valid scalar rejected");
  Require(plan.device_bytes() == 16 && plan.entries()[1].bytes == 4,
          "scalar result mismatch");
  Pass("add_scalar");
  Rejected("zero_extent", plan, [&] { return plan.Add("bad", {2, 0}, 2); });
  Rejected("negative_extent", plan,
           [&] { return plan.Add("bad", {2, -1}, 2); });
  Rejected("zero_element_bytes", plan,
           [&] { return plan.Add("bad", {2, 3}, 0); });
  Rejected("shape_product_overflow", plan, [&] {
    return plan.Add("bad", {std::numeric_limits<int64_t>::max(), 3}, 1);
  });
  Rejected("element_product_overflow", plan, [&] {
    return plan.Add("bad", {2}, std::numeric_limits<size_t>::max());
  });
  Rejected("empty_name", plan, [&] { return plan.Add("", {1}, 1); });
  Rejected("zero_bytes", plan, [&] { return plan.AddBytes("bad", 0); });
  Rejected("duplicate_name_same_size", plan,
           [&] { return plan.Add("matrix", {2, 3}, 2); });
  Rejected("duplicate_name_other_size", plan,
           [&] { return plan.AddBytes("matrix", 1); });
  StartupPlan boundary;
  Require(boundary.AddBytes("large", std::numeric_limits<size_t>::max() - 8)
              .ok(),
          "large metadata size rejected");
  Require(boundary.AddBytes("last", 8).ok() &&
              boundary.device_bytes() == std::numeric_limits<size_t>::max(),
          "exact total boundary rejected");
  Pass("exact_total_size_limit");
  Rejected("total_bytes_overflow", boundary,
           [&] { return boundary.AddBytes("overflow", 1); });
  Rejected("add_shape_total_overflow", boundary,
           [&] { return boundary.Add("overflow", {1}, 1); });
}

void TestMetadata(const std::string& fixture) {
  using q4t::io::Dtype;
  using q4t::runtime::StartupPlan;
  q4t::io::WeightIndex* raw_index = nullptr;
  auto status = q4t::io::WeightIndex::Open(
      fixture + "/model.safetensors.index.json", &raw_index);
  Require(status.ok(), "fixture index: " + status.message());
  std::unique_ptr<q4t::io::WeightIndex> index(raw_index);
  q4t::io::WeightLoader* raw_loader = nullptr;
  status = q4t::io::WeightLoader::Create(fixture, *index, 1, &raw_loader);
  Require(status.ok(), "fixture loader: " + status.message());
  std::unique_ptr<q4t::io::WeightLoader> loader(raw_loader);
  loader->SetKeepPageCache(true, "");
  StartupPlan plan;
  Require(plan.AddBytes("sentinel", 7).ok(), "sentinel rejected");
  const Snapshot original = Save(plan);
  Require(plan.CheckTensor(*loader, "valid", Dtype::kBF16, {2, 3}).ok(),
          "valid tensor rejected");
  Unchanged(plan, original);
  Pass("check_tensor_is_nonmutating");
  Require(plan.CheckTensor(*loader, "scalar", Dtype::kF32, {}).ok(),
          "scalar tensor rejected");
  Unchanged(plan, original);
  Pass("check_scalar_tensor");
  for (const char* name : {"missing", "wrong_shape", "wrong_dtype",
                           "short_span", "long_span", "reversed_span"}) {
    const std::string check = std::string("check_") + name;
    Rejected(check.c_str(), plan, [&] {
      return plan.CheckTensor(*loader, name, Dtype::kBF16, {2, 3});
    });
    const std::string add = std::string("add_tensor_") + name;
    Rejected(add.c_str(), plan, [&] {
      return plan.AddTensor(*loader, name, Dtype::kBF16, {2, 3});
    });
  }
  Rejected("check_zero_expected_shape", plan, [&] {
    return plan.CheckTensor(*loader, "valid", Dtype::kBF16, {0, 3});
  });
  Rejected("check_negative_expected_shape", plan, [&] {
    return plan.CheckTensor(*loader, "valid", Dtype::kBF16, {-1, 3});
  });
  Rejected("check_overflow_expected_shape", plan, [&] {
    return plan.CheckTensor(*loader, "valid", Dtype::kBF16,
                             {std::numeric_limits<int64_t>::max(), 3});
  });
  Require(plan.AddTensor(*loader, "valid", Dtype::kBF16, {2, 3}).ok() &&
              plan.device_bytes() == 19 && plan.entries().size() == 2,
          "valid AddTensor result mismatch");
  Pass("add_tensor_valid");
  Rejected("add_tensor_duplicate", plan, [&] {
    return plan.AddTensor(*loader, "valid", Dtype::kBF16, {2, 3});
  });
  StartupPlan full;
  Require(full.AddBytes("full", std::numeric_limits<size_t>::max()).ok(),
          "full total rejected");
  Rejected("add_tensor_total_overflow", full, [&] {
    return full.AddTensor(*loader, "valid", Dtype::kBF16, {2, 3});
  });

  // Opening B evicts A; then an uncached name from A evicts B. Keep-cache
  // must cover both LRU eviction and final loader destruction.
  const auto* cached = loader->FindTensor("valid");
  Require(cached != nullptr, "valid metadata missing");
  Require(plan.CheckTensor(*loader, "other_shard", Dtype::kBF16, {1}).ok(),
          "other shard rejected");
  Require(plan.CheckTensor(*loader, "uncached_a", Dtype::kBF16, {1}).ok(),
          "reopened shard rejected");
  Require(loader->open_shards() == 1 && loader->FindTensor("valid") == cached,
          "LRU lost cached metadata identity");
  loader.reset();
  Require(cache_advice_calls == 0, "metadata pass issued cache eviction");
  Pass("metadata_keep_cache_across_eviction_and_close");
}
}  // namespace

extern "C" int __wrap_posix_fadvise(int, off_t, off_t, int) {
  ++cache_advice_calls;
  return 0;
}

int main(int argc, char** argv) {
  Require(argc == 2, "usage: test_core FIXTURE_DIRECTORY");
  TestArithmetic();
  TestMetadata(argv[1]);
  std::printf("{\"status\":\"PASS\",\"count\":%d,"
              "\"cache_advice_calls\":%d,\"gpu_api_calls\":0,"
              "\"payload_read_calls\":0}\n",
              cases, cache_advice_calls);
  return 0;
}
