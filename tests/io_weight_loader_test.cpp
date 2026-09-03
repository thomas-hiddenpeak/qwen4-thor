// Tests for weight loading orchestration, against the real model index +
// shards.
#include "q4t/io/weight_loader.h"
#include "q4t/test.h"

#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>

#include <string>
#include <vector>

namespace {

using q4t::Status;
using q4t::io::SafetensorsFile;
using q4t::io::TensorInfo;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";

bool FileExists(const char* path) {
  int fd = open(path, O_RDONLY);
  if (fd < 0) return false;
  close(fd);
  return true;
}

}  // namespace

Q4T_TEST(weight_index_parse_real) {
  if (!FileExists(kIndex)) {
    std::printf("  (skipped: real index.json not present)\n");
    return true;
  }
  WeightIndex* idx = nullptr;
  Status s = WeightIndex::Open(kIndex, &idx);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(idx->num_tensors() == 296347);
  Q4T_CHECK(idx->total_size() == 83995036096u);
  Q4T_CHECK(idx->Has("model.language_model.embed_tokens.weight"));
  Q4T_CHECK(idx->Has("lm_head.weight"));
  Q4T_CHECK(!idx->Has("no.such.tensor"));

  const std::string* shard =
      idx->ShardOf("model.language_model.hyper_connection_mixer.hc_norm.weight");
  Q4T_CHECK(shard != nullptr);
  Q4T_CHECK(*shard == "model-bf16-00001.safetensors");

  // ShardGroups should partition all tensors across 197 shards.
  auto groups = idx->ShardGroups();
  Q4T_CHECK(groups.size() == 197);
  size_t total = 0;
  for (const auto& g : groups) total += g.second.size();
  Q4T_CHECK(total == idx->num_tensors());

  delete idx;
  return true;
}

Q4T_TEST(weight_loader_read_matches_direct_shard) {
  if (!FileExists(kIndex)) {
    std::printf("  (skipped: real index.json not present)\n");
    return true;
  }
  WeightIndex* idx = nullptr;
  Q4T_CHECK(WeightIndex::Open(kIndex, &idx).ok());

  WeightLoader* loader = nullptr;
  Q4T_CHECK(WeightLoader::Create(kModelDir, *idx, 8, &loader).ok());

  const std::string name =
      "model.language_model.hyper_connection_mixer.hc_norm.weight";
  const TensorInfo* info = loader->FindTensor(name);
  Q4T_CHECK(info != nullptr);
  Q4T_CHECK(info->byte_size() > 0);

  // Read via the loader.
  std::vector<uint8_t> via_loader(info->byte_size());
  Q4T_CHECK(loader->ReadTensor(name, via_loader.data()).ok());

  // Read the same tensor by opening its shard directly, and compare.
  const std::string* shard = idx->ShardOf(name);
  Q4T_CHECK(shard != nullptr);
  SafetensorsFile* direct = nullptr;
  Q4T_CHECK(SafetensorsFile::Open(std::string(kModelDir) + "/" + *shard,
                                  &direct)
                .ok());
  const TensorInfo* dinfo = direct->Find(name);
  Q4T_CHECK(dinfo != nullptr);
  std::vector<uint8_t> via_direct(dinfo->byte_size());
  Q4T_CHECK(direct->ReadTensor(*dinfo, via_direct.data()).ok());

  Q4T_CHECK(via_loader.size() == via_direct.size());
  Q4T_CHECK(std::memcmp(via_loader.data(), via_direct.data(),
                        via_direct.size()) == 0);

  // Sanity: a norm weight should not be all-zero.
  bool any_nonzero = false;
  for (uint8_t b : via_direct) {
    if (b != 0) {
      any_nonzero = true;
      break;
    }
  }
  Q4T_CHECK(any_nonzero);

  delete direct;
  delete loader;
  delete idx;
  return true;
}

Q4T_TEST(weight_loader_lru_eviction) {
  if (!FileExists(kIndex)) {
    std::printf("  (skipped: real index.json not present)\n");
    return true;
  }
  WeightIndex* idx = nullptr;
  Q4T_CHECK(WeightIndex::Open(kIndex, &idx).ok());

  // Capacity 1: opening a second shard must evict the first.
  WeightLoader* loader = nullptr;
  Q4T_CHECK(WeightLoader::Create(kModelDir, *idx, 1, &loader).ok());

  const std::string a =
      "model.language_model.hyper_connection_mixer.hc_norm.weight";  // shard 00001
  const std::string b = "lm_head.weight";  // shard 00012
  Q4T_CHECK(loader->FindTensor(a) != nullptr);
  Q4T_CHECK(loader->open_shards() == 1);
  Q4T_CHECK(loader->FindTensor(b) != nullptr);
  Q4T_CHECK(loader->open_shards() == 1);  // evicted a, opened b

  delete loader;
  delete idx;
  return true;
}
