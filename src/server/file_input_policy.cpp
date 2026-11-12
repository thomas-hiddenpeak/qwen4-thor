#include "q4t/server/file_input_policy.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <charconv>
#include <cstdio>
#include <map>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "q4t/trace/sha256.h"
#include "q4t/vision/media_plan.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kPolicyBytes = 64 * 1024;

// The same inventory drives numeric parsing, hard bounds and discovery.
// Paths, credentials and executable names are deliberately absent.
template <typename Options, typename Visitor>
void Numbers(Options& o, Visitor visit) {
  constexpr uint64_t mib = 1ULL << 20;
  visit("", "max_source_bytes", o.max_source_bytes, 1, kFileInputBytes);
  visit("", "max_files", o.max_files, 1, 4096);
  visit("", "max_rendered_bytes", o.max_rendered_bytes, 1, 512 * mib);
  visit("", "max_retained_bytes", o.max_retained_bytes, 1, 1024 * mib);
  visit("", "hold_seconds", o.hold_seconds, 1, 3600);
  visit("", "summary_output_tokens", o.summary_output_tokens, 64, 65536);
  visit("", "total_summary_output_tokens", o.total_summary_output_tokens, 64,
        65536);
  visit("", "extraction_timeout_ms", o.extraction.timeout_ms, 1, 120000);
  auto& d = o.extraction.document;
  visit("document", "max_text_bytes", d.max_text_bytes, 1, 16 * mib);
  visit("document", "max_cell_bytes", d.max_cell_bytes, 1, 4 * mib);
  visit("document", "max_columns", d.max_columns, 1, 16384);
  visit("document", "max_cells", d.max_cells, 1, 1000000);
  visit("document", "max_sheets", d.max_sheets, 1, 256);
  visit("document", "max_nested_documents", d.max_nested_documents, 0, 4);
  visit("document", "max_embedded_documents", d.max_embedded_documents, 1,
        1024);
  visit("document", "timeout_ms", d.timeout_ms, 1, 120000);
  auto& p = d.package;
  visit("package", "max_bytes", p.max_bytes, 1, 512 * mib);
  visit("package", "max_entry_bytes", p.max_entry_bytes, 1, 64 * mib);
  visit("package", "max_entries", p.max_entries, 1, 65536);
  visit("package", "max_xml_nodes", p.max_xml_nodes, 1, 2000000);
  visit("package", "max_xml_depth", p.max_xml_depth, 1, 128);
  auto& pdf = o.extraction.pdf;
  visit("pdf", "max_pages", pdf.max_pages, 1, 256);
  visit("pdf", "max_pixels", pdf.max_pixels, 1, 256 * mib);
  visit("pdf", "max_image_bytes", pdf.max_image_bytes, 1, 256 * mib);
  visit("pdf", "timeout_ms", pdf.timeout_ms, 1, 120000);
  auto& legacy = o.extraction.legacy;
  visit("legacy", "max_converted_bytes", legacy.max_converted_bytes, 1,
        kFileInputBytes);
  visit("legacy", "timeout_ms", legacy.timeout_ms, 1, 120000);
  visit("legacy_io", "write_bytes", legacy.io.write_bytes, 0, 1024 * mib);
  visit("legacy_io", "file_creations", legacy.io.file_creations, 0, 65536);
  visit("legacy_io", "thread_creations", legacy.io.thread_creations, 0, 256);
  visit("legacy_io", "notifications", legacy.io.notifications, 1, 2000000);
  auto& image = o.image;
  visit("image", "max_bytes", image.max_bytes, 1, kFileInputBytes);
  visit("image", "max_pixels", image.max_pixels, 1, 256 * mib);
  visit("image", "max_edge", image.max_edge, 1, 65536);
  visit("image", "max_conversion_bytes", image.max_conversion_bytes, 1024,
        256 * mib);
  visit("image", "codec_timeout_ms", image.codec_timeout_ms, 1, 30000);
  visit("storage", "max_bytes", o.storage.max_bytes, 1, 512 * mib);
  visit("storage", "max_metadata_bytes", o.storage.max_metadata_bytes, 1,
        32 * mib);
  visit("storage", "max_parts", o.storage.max_parts, 1, 1024);
}
Status Invalid(std::string field = {}) {
  return Status::Fail("Invalid file input policy" +
                      (field.empty() ? "" : ": " + field));
}
std::string Field(std::string_view group, std::string_view name) {
  return group.empty() ? std::string(name)
                       : std::string(group) + "." + std::string(name);
}
bool Unsigned(const Json& value, uint64_t* result) {
  if (!value.IsNumber() || value.str.empty()) return false;
  const auto parsed = std::from_chars(
      value.str.data(), value.str.data() + value.str.size(), *result);
  return parsed.ec == std::errc() &&
         parsed.ptr == value.str.data() + value.str.size();
}
}  // namespace

bool ValidFileExpansionOptions(const FileExpansionOptions& options) {
  bool valid = true;
  Numbers(options, [&](auto, auto, auto value, uint64_t low, uint64_t high) {
    valid &= static_cast<uint64_t>(value) >= low &&
             static_cast<uint64_t>(value) <= high;
  });
  const auto& path = options.extraction.legacy.work_directory;
  return valid && file_detail::ValidOptions(options.fetch) &&
         ValidFileDocumentLimits(options.extraction.document) &&
         options.storage.max_metadata_bytes <= options.storage.max_bytes &&
         (path.empty() || (path.front() == '/' && path.size() <= 4096 &&
                           path.find('\0') == path.npos));
}

Status ParseFileInputPolicy(std::string_view raw, FileInputPolicy* output) {
  if (!output || raw.empty() || raw.size() > kPolicyBytes) return Invalid();
  Json value;
  if (!io::ParseJson(std::string(raw), &value, true,
                     {.max_depth = 5,
                      .max_values = 4096,
                      .max_string_bytes = kPolicyBytes,
                      .reject_duplicate_keys = true}) ||
      !value.IsObject())
    return Invalid();
  const auto* version = value.Find("version");
  if (!version || !version->IsNumber() || version->str != "1")
    return Invalid("version");
  FileInputPolicy next;
  std::map<std::string, std::set<std::string>> allowed;
  allowed[""] = {"version", "fetch"};
  Status status;
  Numbers(next.options, [&](const char* group, const char* name, auto& target,
                            uint64_t low, uint64_t high) {
    allowed[group].insert(name);
    if (*group) allowed[""].insert(group);
    if (!status) return;
    const auto* parent = *group ? value.Find(group) : &value;
    if (!parent) return;
    if (!parent->IsObject()) {
      status = Invalid(group);
      return;
    }
    if (const auto* item = parent->Find(name)) {
      uint64_t number = 0;
      if (!Unsigned(*item, &number) || number < low || number > high)
        status = Invalid(Field(group, name));
      else
        target = number;
    }
  });
  if (!status) return status;
  for (const auto& [group, names] : allowed) {
    const auto* parent = group.empty() ? &value : value.Find(group);
    if (!parent) continue;
    for (const auto& [name, _] : parent->object)
      if (!names.contains(name)) return Invalid(Field(group, name));
  }
  if (const auto* fetch = value.Find("fetch")) {
    const auto reply = ParseFileFetchOptions(*fetch, &next.options.fetch);
    if (!reply.ok()) return Invalid("fetch");
  }
  if (!ValidFileExpansionOptions(next.options)) return Invalid();
  next.sha256 = trace::HexDigest(trace::Sha256(
      {reinterpret_cast<const uint8_t*>(raw.data()), raw.size()}));
  *output = std::move(next);
  return {};
}

Status LoadFileInputPolicy(const std::string& path, FileInputPolicy* output) {
  if (path.empty() || path.size() > 4096 || path.find('\0') != path.npos)
    return Invalid();
  const int fd =
      ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  if (fd < 0) return Invalid();
  struct Close {
    int fd;
    ~Close() { ::close(fd); }
  } close{fd};
  struct stat before {
  }, after{};
  if (::fstat(fd, &before) || !S_ISREG(before.st_mode) ||
      before.st_uid != ::geteuid() || (before.st_mode & 0077) ||
      before.st_nlink != 1 || before.st_size <= 0 ||
      static_cast<uint64_t>(before.st_size) > kPolicyBytes)
    return Invalid();
  std::string raw(static_cast<size_t>(before.st_size) + 1, '\0');
  size_t count = 0;
  while (count < raw.size()) {
    const auto got = ::read(fd, raw.data() + count, raw.size() - count);
    if (got < 0 && errno == EINTR) continue;
    if (got < 0) return Invalid();
    if (!got) break;
    count += static_cast<size_t>(got);
  }
  if (::fstat(fd, &after) || count != static_cast<size_t>(before.st_size) ||
      before.st_dev != after.st_dev || before.st_ino != after.st_ino ||
      before.st_size != after.st_size || before.st_mode != after.st_mode ||
      before.st_uid != after.st_uid || before.st_nlink != after.st_nlink ||
      before.st_mtim.tv_sec != after.st_mtim.tv_sec ||
      before.st_mtim.tv_nsec != after.st_mtim.tv_nsec ||
      before.st_ctim.tv_sec != after.st_ctim.tv_sec ||
      before.st_ctim.tv_nsec != after.st_ctim.tv_nsec)
    return Invalid();
  raw.resize(count);
  return ParseFileInputPolicy(raw, output);
}

Json FileInputPolicyDescription(const FileExpansionOptions& options,
                                std::string_view sha256) {
  std::map<std::string, Json> groups;
  Numbers(options,
          [&](const char* group, const char* name, auto value, auto, auto) {
            auto [entry, _] = groups.try_emplace(group, Object());
            Set(&entry->second, name, Number(value));
          });
  auto result = std::move(groups[""]);
  for (auto& [group, value] : groups)
    if (!group.empty()) Set(&result, group, std::move(value));
  const auto& fetch = options.fetch;
  auto ports = Array();
  for (int port : fetch.ports) ports.array.push_back(Number(port));
  Set(&result, "fetch",
      Object({{"public_networks", Boolean(fetch.public_networks)},
              {"allow_http", Boolean(fetch.allow_http)},
              {"allowed_networks_count", Number(fetch.allowed_networks.size())},
              {"allowed_hosts_count", Number(fetch.allowed_hosts.size())},
              {"ports", std::move(ports)},
              {"custom_ca", Boolean(!fetch.ca_file.empty())},
              {"timeout_ms", Number(fetch.timeout_ms)},
              {"max_redirects", Number(fetch.max_redirects)}}));
  Set(&result, "profile", String("q4t-file-input-policy-v1"));
  Set(&result, "sha256", sha256.empty() ? Json{} : String(std::string(sha256)));
  Set(&result, "configured", Boolean(!sha256.empty()));
  Set(&result, "request_override", Boolean(false));
  Set(&result, "model_media_items", Number(vision::kMediaPlanItems));
  Set(&result, "spreadsheet_rows", Number(FileDocumentLimits::kSheetRows));
  Set(&result, "retained_budget_is_rss_limit", Boolean(false));
  Set(&result, "body_storage_context_admission_separate", Boolean(true));
  return result;
}

int RunFileInputPolicyCheck(int argc, char** argv) {
  if (argc != 3) {
    std::fprintf(stderr, "Usage: q4t file-input-policy-check FILE\n");
    return 2;
  }
  FileInputPolicy policy;
  const auto status = LoadFileInputPolicy(argv[2], &policy);
  if (!status) {
    std::fprintf(stderr, "%s\n", status.message().c_str());
    return 2;
  }
  const auto json =
      OutputJson(FileInputPolicyDescription(policy.options, policy.sha256));
  std::printf("%s\n", json.c_str());
  return 0;
}
}  // namespace q4t::server
