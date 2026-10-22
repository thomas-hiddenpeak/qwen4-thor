#include "q4t/mtp/subset_head.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iterator>

#include "q4t/io/json.h"
#include "q4t/trace/sha256.h"

namespace q4t::mtp {
namespace {
Status ReadJson(const std::string& path, io::Json* result) {
  std::ifstream input(path);
  if (!input) return Status::Fail("subset head: cannot open " + path);
  std::string text{std::istreambuf_iterator<char>(input), {}};
  io::JsonParseLimits limits;
  limits.reject_duplicate_keys = true;
  return io::ParseJson(text, result, false, limits);
}
Status CheckHash(const std::string& path, const std::string& expected) {
  trace::Digest digest;
  Status status = trace::HashFile(path, &digest);
  if (!status.ok()) return status;
  if (expected.size() != 64 || trace::HexDigest(digest) != expected)
    return Status::Fail("subset head: SHA256 mismatch: " + path);
  return Status();
}
template <typename T>
Status ReadVector(const std::string& path, size_t count,
                  const std::string& expected, std::vector<T>* result) {
  std::ifstream input(path, std::ios::binary | std::ios::ate);
  if (!input || input.tellg() != static_cast<std::streamoff>(count * sizeof(T)))
    return Status::Fail("subset head: incorrect file size: " + path);
  result->resize(count);
  input.seekg(0);
  if (!input.read(reinterpret_cast<char*>(result->data()), count * sizeof(T)))
    return Status::Fail("subset head: read failed: " + path);
  const auto bytes = std::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(result->data()), count * sizeof(T));
  if (trace::HexDigest(trace::Sha256(bytes)) != expected)
    return Status::Fail("subset head: payload digest mismatch: " + path);
  return Status();
}
}  // namespace

Status ReadSubsetHead(const std::string& directory,
                      const std::string& model_directory, int vocab, int hs,
                      SubsetHeadData* output) {
  if (!output || directory.empty())
    return Status::Fail("subset head: missing directory or output");
  io::Json manifest;
  Status status = ReadJson(directory + "/manifest.json", &manifest);
  if (!status.ok()) return status;
  if (manifest.GetInt("version") != 1 || manifest.GetInt("rows") != kSubsetRows ||
      manifest.GetInt("vocab") != vocab || manifest.GetInt("hidden") != hs ||
      vocab != 248320 || hs != 2560 || manifest.GetString("dtype") != "BF16")
    return Status::Fail("subset head: unsupported format or dimensions");
  io::Json index;
  status = ReadJson(model_directory + "/model.safetensors.index.json", &index);
  if (!status.ok()) return status;
  const io::Json* map = index.Find("weight_map");
  const std::string shard = manifest.GetString("source_shard");
  if (!map || shard.empty() || std::filesystem::path(shard).filename() != shard ||
      map->GetString("lm_head.weight") != shard)
    return Status::Fail("subset head: source head identity mismatch");
  size_t cache_bytes = 0;
  for (const std::string& path : {
           model_directory + "/" + shard,
           model_directory + "/tokenizer.json",
           model_directory + "/tokenizer_config.json",
           model_directory + "/generation_config.json",
           model_directory + "/model.safetensors.index.json",
           directory + "/manifest.json", directory + "/head.bf16",
           directory + "/token_ids.bin"}) {
    std::error_code error;
    const auto bytes = std::filesystem::file_size(path, error);
    if (error || bytes > kSubsetReadCacheBudget - cache_bytes)
      return Status::Fail("subset head: validation file-cache budget exceeded");
    cache_bytes += bytes;
  }
  status = CheckHash(model_directory + "/" + shard,
                      manifest.GetString("source_sha256"));
  if (!status.ok()) return status;
  for (const char* name : {"tokenizer", "tokenizer_config", "generation_config"}) {
    status = CheckHash(model_directory + "/" + name + ".json",
                        manifest.GetString(std::string(name) + "_sha256"));
    if (!status.ok()) return status;
  }
  SubsetHeadData data;
  status = ReadVector(directory + "/token_ids.bin", kSubsetRows,
                       manifest.GetString("ids_sha256"), &data.ids);
  if (!status.ok()) return status;
  int previous = -1;
  for (int id : data.ids) {
    if (id <= previous || id >= vocab)
      return Status::Fail("subset head: IDs must be sorted, unique and legal");
    previous = id;
  }
  // Derive required special IDs independently from the actual tokenizer.
  io::Json tokenizer;
  status = ReadJson(model_directory + "/tokenizer_config.json", &tokenizer);
  if (!status.ok()) return status;
  const io::Json* added = tokenizer.Find("added_tokens_decoder");
  if (!added || !added->IsObject())
    return Status::Fail("subset head: tokenizer added tokens missing");
  auto included = [&](int64_t id) {
    return id >= 0 && id < vocab &&
           std::binary_search(data.ids.begin(), data.ids.end(), id);
  };
  for (const auto& [key, value] : added->object) {
    if (!value.GetBool("special")) continue;
    io::Json id;
    status = io::ParseJson(key, &id);
    if (!status.ok() || !included(id.AsInt(-1)))
      return Status::Fail("subset head: missing tokenizer special ID");
  }
  io::Json generation;
  status = ReadJson(model_directory + "/generation_config.json", &generation);
  if (!status.ok()) return status;
  for (const char* name : {"eos_token_id", "bos_token_id", "pad_token_id"}) {
    const io::Json* value = generation.Find(name);
    if (!value || value->IsNull()) continue;
    if (value->IsArray()) {
      for (const auto& id : value->array)
        if (!included(id.AsInt(-1)))
          return Status::Fail("subset head: missing generation special ID");
    } else if (!included(value->AsInt(-1))) {
      return Status::Fail("subset head: missing generation special ID");
    }
  }
  status = ReadVector(directory + "/head.bf16", kSubsetWeightBytes / 2,
                       manifest.GetString("head_sha256"), &data.weights);
  if (!status.ok()) return status;
  *output = std::move(data);
  return Status();
}
}  // namespace q4t::mtp
