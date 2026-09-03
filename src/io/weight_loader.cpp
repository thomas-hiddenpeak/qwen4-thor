// Weight loading orchestration implementation.
#include "q4t/io/weight_loader.h"

#include <deque>
#include <fstream>
#include <sstream>

#include "q4t/io/json.h"

namespace q4t {
namespace io {

namespace {

Status ReadFile(const std::string& path, std::string* out) {
  std::ifstream in(path, std::ios::binary);
  if (!in) return Status::Fail("cannot open " + path);
  std::ostringstream ss;
  ss << in.rdbuf();
  *out = ss.str();
  return Status();
}

}  // namespace

// ---------------------------------------------------------------------------
// WeightIndex
// ---------------------------------------------------------------------------
struct WeightIndex::Impl {
  std::unordered_map<std::string, std::string> weight_map;
  uint64_t total_size = 0;
  // shard -> tensors (in index order).
  std::vector<std::pair<std::string, std::vector<std::string>>> groups;
  std::unordered_map<std::string, size_t> group_index;
};

WeightIndex::WeightIndex(Impl* impl) : impl_(impl) {}

WeightIndex::~WeightIndex() {
  delete impl_;
  impl_ = nullptr;
}

Status WeightIndex::Open(const std::string& path, WeightIndex** out) {
  *out = nullptr;
  std::string text;
  Status s = ReadFile(path, &text);
  if (!s.ok()) return s;

  Json root;
  s = ParseJson(text, &root);
  if (!s.ok()) return s;
  if (!root.IsObject()) return Status::Fail("index.json is not an object");

  const Json* wm = root.GetArray("weight_map");
  if (!wm && (wm = root.Find("weight_map")) && !wm->IsObject()) {
    return Status::Fail("index.json missing weight_map object");
  }
  if (!wm || !wm->IsObject()) {
    return Status::Fail("index.json missing weight_map object");
  }

  Impl* impl = new Impl();
  impl->weight_map.reserve(wm->object.size());
  for (const auto& kv : wm->object) {
    if (!kv.second.IsString()) {
      delete impl;
      return Status::Fail("weight_map value is not a string: " + kv.first);
    }
    impl->weight_map.emplace(kv.first, kv.second.str);
    auto it = impl->group_index.find(kv.second.str);
    if (it == impl->group_index.end()) {
      it = impl->group_index.emplace(kv.second.str, impl->groups.size()).first;
      impl->groups.emplace_back(kv.second.str, std::vector<std::string>());
    }
    impl->groups[it->second].second.push_back(kv.first);
  }

  const Json* meta = root.Find("metadata");
  if (meta && meta->IsObject()) {
    impl->total_size = static_cast<uint64_t>(meta->GetNumber("total_size"));
  }

  *out = new WeightIndex(impl);
  return Status();
}

const std::string* WeightIndex::ShardOf(const std::string& name) const {
  auto it = impl_->weight_map.find(name);
  return it == impl_->weight_map.end() ? nullptr : &it->second;
}

bool WeightIndex::Has(const std::string& name) const {
  return impl_->weight_map.find(name) != impl_->weight_map.end();
}

size_t WeightIndex::num_tensors() const { return impl_->weight_map.size(); }

uint64_t WeightIndex::total_size() const { return impl_->total_size; }

std::vector<std::pair<std::string, std::vector<std::string>>>
WeightIndex::ShardGroups() const {
  return impl_->groups;
}

// ---------------------------------------------------------------------------
// WeightLoader
// ---------------------------------------------------------------------------
struct WeightLoader::Impl {
  std::string model_dir;
  const WeightIndex* index = nullptr;
  size_t max_open = 8;

  struct Shard {
    std::unique_ptr<SafetensorsFile> file;
  };
  // Mutable: EnsureOpen is logically const (no observable change to the
  // loader's contract) but manages the open-shard cache.
  mutable std::unordered_map<std::string, Shard> open;
  mutable std::deque<std::string> lru;  // most-recent at back

  Status EnsureOpen(const std::string& shard, Shard** out) const;
};

Status WeightLoader::Impl::EnsureOpen(const std::string& shard,
                                      Shard** out) const {
  auto it = open.find(shard);
  if (it != open.end()) {
    // Move to MRU.
    for (auto l = lru.begin(); l != lru.end(); ++l) {
      if (*l == shard) {
        lru.erase(l);
        break;
      }
    }
    lru.push_back(shard);
    *out = &it->second;
    return Status();
  }

  // Evict LRU if at capacity.
  while (open.size() >= max_open && !lru.empty()) {
    const std::string victim = lru.front();
    lru.pop_front();
    open.erase(victim);
  }

  const std::string path = model_dir + "/" + shard;
  SafetensorsFile* f = nullptr;
  Status s = SafetensorsFile::Open(path, &f);
  if (!s.ok()) return s;
  Shard sh;
  sh.file.reset(f);
  auto inserted = open.emplace(shard, std::move(sh));
  lru.push_back(shard);
  *out = &inserted.first->second;
  return Status();
}

WeightLoader::WeightLoader(Impl* impl) : impl_(impl) {}

WeightLoader::~WeightLoader() {
  delete impl_;
  impl_ = nullptr;
}

Status WeightLoader::Create(const std::string& model_dir,
                            const WeightIndex& index, size_t max_open_shards,
                            WeightLoader** out) {
  *out = nullptr;
  if (max_open_shards == 0) max_open_shards = 1;
  Impl* impl = new Impl();
  impl->model_dir = model_dir;
  impl->index = &index;
  impl->max_open = max_open_shards;
  *out = new WeightLoader(impl);
  return Status();
}

const TensorInfo* WeightLoader::FindTensor(const std::string& name) const {
  const std::string* shard = impl_->index->ShardOf(name);
  if (!shard) return nullptr;
  Impl::Shard* sh = nullptr;
  if (!impl_->EnsureOpen(*shard, &sh).ok()) return nullptr;
  return sh->file->Find(name);
}

Status WeightLoader::ReadTensor(const std::string& name, void* dst) const {
  const std::string* shard = impl_->index->ShardOf(name);
  if (!shard) return Status::Fail("tensor not in index: " + name);
  Impl::Shard* sh = nullptr;
  Status s = impl_->EnsureOpen(*shard, &sh);
  if (!s.ok()) return s;
  const TensorInfo* info = sh->file->Find(name);
  if (!info) return Status::Fail("tensor not in shard " + *shard + ": " + name);
  return sh->file->ReadTensor(*info, dst);
}

Status WeightLoader::ReadTensorToDevice(const std::string& name, void* dst,
                                        void* device_dst,
                                        cudaStream_t stream) const {
  const std::string* shard = impl_->index->ShardOf(name);
  if (!shard) return Status::Fail("tensor not in index: " + name);
  Impl::Shard* sh = nullptr;
  Status s = impl_->EnsureOpen(*shard, &sh);
  if (!s.ok()) return s;
  const TensorInfo* info = sh->file->Find(name);
  if (!info) return Status::Fail("tensor not in shard " + *shard + ": " + name);
  return sh->file->ReadTensorToDevice(*info, dst, device_dst, stream);
}

size_t WeightLoader::open_shards() const { return impl_->open.size(); }

}  // namespace io
}  // namespace q4t
