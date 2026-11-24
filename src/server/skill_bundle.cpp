#include "skill_bundle.h"

#include <dlfcn.h>

#include <algorithm>
#include <set>

#include "file_package_runtime.h"
#include "object_protocol.h"
#include "q4t/server/lifetime.h"

namespace q4t::server::skill_detail {
namespace {
using object_detail::Error;
struct ZipWriter {
#define Q4T_API(name) decltype(&name) name##_fn = nullptr
  Q4T_API(archive_write_new);
  Q4T_API(archive_write_set_format_zip);
  Q4T_API(archive_write_set_bytes_per_block);
  Q4T_API(archive_write_open);
  Q4T_API(archive_write_header);
  Q4T_API(archive_write_data);
  Q4T_API(archive_write_close);
  Q4T_API(archive_write_free);
  Q4T_API(archive_entry_new);
  Q4T_API(archive_entry_free);
  Q4T_API(archive_entry_set_pathname_utf8);
  Q4T_API(archive_entry_set_size);
  Q4T_API(archive_entry_set_filetype);
  Q4T_API(archive_entry_set_perm);
#undef Q4T_API
  bool ready = false;
};
ZipWriter& Writer() {
  static ZipWriter writer;
  return writer;
}
ObjectReply Invalid() {
  return Error(400, "invalid_skill_bundle",
               "Expected one skill root and SKILL.md");
}
ObjectReply Capacity() {
  return Error(413, "skill_bundle_capacity", "Skill bundle exceeds capacity");
}
ObjectReply WriterUnavailable() {
  return Error(503, "skill_archive_writer_unavailable",
               "Skill ZIP writer unavailable");
}
ObjectReply CheckRequest(const BinaryAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Skill processing cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Skill processing timed out");
  return {};
}
bool IsManifest(std::string_view leaf) {
  constexpr std::string_view name = "skill.md";
  if (leaf.size() != name.size()) return false;
  for (size_t i = 0; i < name.size(); ++i) {
    char c = leaf[i];
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + ('a' - 'A'));
    if (c != name[i]) return false;
  }
  return true;
}
ObjectReply Inspect(const file_detail::Package& files,
                    const BinaryAccess& access, Manifest* output) {
  if (files.empty()) return Invalid();
  if (files.size() > kSkillMaxFiles) return Capacity();
  std::string_view root;
  const file_detail::PackageEntry* manifest = nullptr;
  std::set<std::string_view> names, directories;
  size_t total = 0;
  for (const auto& entry : files) {
    if (auto status = CheckRequest(access); !status.ok()) return status;
    const std::string_view name = entry.name;
    const auto slash = name.find('/');
    if (!file_detail::ValidPackageMemberName(name, false) ||
        slash == name.npos || directories.contains(name) ||
        !names.insert(name).second)
      return Invalid();
    if (root.empty()) root = name.substr(0, slash);
    if (name.substr(0, slash) != root) return Invalid();
    for (size_t i = slash; i != name.npos; i = name.find('/', i + 1)) {
      const auto prefix = name.substr(0, i);
      if (names.contains(prefix)) return Invalid();
      directories.insert(prefix);
    }
    if (entry.bytes.size() > kSkillFileBytes ||
        entry.bytes.size() > file_detail::PackageLimits{}.max_bytes - total)
      return Capacity();
    total += entry.bytes.size();
    if (IsManifest(name.substr(name.rfind('/') + 1))) {
      if (manifest || name.find('/', slash + 1) != name.npos) return Invalid();
      manifest = &entry;
    }
  }
  if (!manifest) return Invalid();
  return ReadManifest(root, manifest->bytes, output);
}
struct Sink {
  std::string bytes;
  bool full = false;
};
la_ssize_t WriteBytes(archive*, void* context, const void* block,
                      size_t count) {
  auto* sink = static_cast<Sink*>(context);
  if (count > kSkillArchiveBytes - sink->bytes.size()) {
    sink->full = true;
    return -1;
  }
  try {
    sink->bytes.append(static_cast<const char*>(block), count);
  } catch (...) {
    return -1;
  }
  return static_cast<la_ssize_t>(count);
}
}  // namespace

ObjectReply LoadSkillRuntime(bool write_zip) {
  auto loaded = file_detail::LoadArchiveRuntime();
  if (!loaded.ok()) return loaded;
  loaded = LoadManifestRuntime();
  if (!loaded.ok() || !write_zip || Writer().ready) return loaded;
  auto& api = Writer();
  void* handle =
      dlopen(Q4T_SYSTEM_ARCHIVE_LIBRARY, RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
  if (!handle) return WriterUnavailable();
  ScopeExit close([&] { dlclose(handle); });
#define Q4T_LOAD(symbol)                                                   \
  api.symbol##_fn =                                                        \
      reinterpret_cast<decltype(api.symbol##_fn)>(dlsym(handle, #symbol)); \
  if (!api.symbol##_fn) return WriterUnavailable()
  Q4T_LOAD(archive_write_new);
  Q4T_LOAD(archive_write_set_format_zip);
  Q4T_LOAD(archive_write_set_bytes_per_block);
  Q4T_LOAD(archive_write_open);
  Q4T_LOAD(archive_write_header);
  Q4T_LOAD(archive_write_data);
  Q4T_LOAD(archive_write_close);
  Q4T_LOAD(archive_write_free);
  Q4T_LOAD(archive_entry_new);
  Q4T_LOAD(archive_entry_free);
  Q4T_LOAD(archive_entry_set_pathname_utf8);
  Q4T_LOAD(archive_entry_set_size);
  Q4T_LOAD(archive_entry_set_filetype);
  Q4T_LOAD(archive_entry_set_perm);
#undef Q4T_LOAD
  api.ready = true;
  return {};
}

ObjectReply ReadSkillArchive(std::string archive, const BinaryAccess& access,
                             Bundle* output) {
  if (!output || archive.empty()) return Invalid();
  if (archive.size() > kSkillArchiveBytes) return Capacity();
  file_detail::PackageLimits limits;
  // Directory entries do not count toward the documented file limit. The
  // package reader separately bounds all administrative entries.
  limits.max_entry_bytes = kSkillFileBytes;
  Bundle next;
  std::vector<std::string> directories;
  auto parsed = file_detail::ReadPackage(archive, limits, access, &next.members,
                                         &directories);
  if (!parsed.ok()) return parsed;
  auto inspected = Inspect(next.members, access, &next.manifest);
  if (!inspected.ok()) return inspected;
  std::set<std::string_view> files;
  for (const auto& member : next.members) files.insert(member.name);
  for (const auto& dir : directories) {
    std::string_view name = dir;
    if (name.substr(0, name.find('/')) != next.manifest.name) return Invalid();
    for (auto slash = name.find('/'); slash != name.npos;
         slash = name.find('/', slash + 1))
      if (files.contains(name.substr(0, slash))) return Invalid();
  }
  next.archive = std::move(archive);
  *output = std::move(next);
  return {};
}
ObjectReply MakeSkillArchive(file_detail::Package files,
                             const BinaryAccess& access, Bundle* output) {
  if (!output) return Invalid();
  Bundle next;
  auto inspected = Inspect(files, access, &next.manifest);
  if (!inspected.ok()) return inspected;
  const auto& api = Writer();
  if (!api.ready) return WriterUnavailable();
  Sink sink;
  archive* archive = api.archive_write_new_fn();
  if (!archive) return WriterUnavailable();
  ScopeExit finish([&] { api.archive_write_free_fn(archive); });
  if (api.archive_write_set_format_zip_fn(archive) != ARCHIVE_OK ||
      api.archive_write_set_bytes_per_block_fn(archive, 0) != ARCHIVE_OK ||
      api.archive_write_open_fn(archive, &sink, nullptr, WriteBytes, nullptr) !=
          ARCHIVE_OK)
    return WriterUnavailable();
  for (const auto& file : files) {
    if (auto live = CheckRequest(access); !live.ok()) return live;
    auto* entry = api.archive_entry_new_fn();
    if (!entry) return WriterUnavailable();
    ScopeExit free([&] { api.archive_entry_free_fn(entry); });
    api.archive_entry_set_pathname_utf8_fn(entry, file.name.c_str());
    api.archive_entry_set_filetype_fn(entry, AE_IFREG);
    api.archive_entry_set_perm_fn(entry, 0644);
    api.archive_entry_set_size_fn(entry, file.bytes.size());
    if (api.archive_write_header_fn(archive, entry) != ARCHIVE_OK ||
        api.archive_write_data_fn(archive, file.bytes.data(),
                                  file.bytes.size()) !=
            static_cast<la_ssize_t>(file.bytes.size()))
      return sink.full ? Capacity() : WriterUnavailable();
  }
  if (api.archive_write_close_fn(archive) != ARCHIVE_OK)
    return sink.full ? Capacity() : WriterUnavailable();
  next.archive = std::move(sink.bytes);
  next.members = std::move(files);
  *output = std::move(next);
  return {};
}
}  // namespace q4t::server::skill_detail
