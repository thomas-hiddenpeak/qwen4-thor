#include "q4t/server/skill_worker.h"

#include <algorithm>
#include <chrono>
#include <cstring>

#include "object_protocol.h"
#include "q4t/server/file_document_worker.h"
#include "skill_bundle.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace skill_detail;
using Clock = std::chrono::steady_clock;
using Transfer = std::function<bool(void*, size_t, bool)>;
constexpr size_t kControlBytes = 1U << 20;

ObjectReply Failure(GrammarFailure cause) {
  if (cause == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Skill processing cancelled");
  if (cause == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded",
                 "Skill processing timed out");
  if (cause == GrammarFailure::kUnavailable)
    return Error(503, "skill_worker_capacity",
                 "Skill processing capacity occupied");
  return Error(503, "skill_worker_failed",
               "Skill processing worker unavailable");
}
bool Metadata(const Transfer& transfer, bool write, uint64_t sequence,
              Json* value) {
  std::string text = write ? OutputJson(*value) : std::string();
  Frame header{kVersion, sequence, text.size()};
  if (!transfer(&header, sizeof(header), write) || header.version != kVersion ||
      header.sequence != sequence || header.bytes == 0 ||
      header.bytes > kControlBytes)
    return false;
  text.resize(header.bytes);
  return transfer(text.data(), text.size(), write) &&
         (write || Parse(text, value));
}
Json Layout(const std::vector<FilePackageEntry>& files) {
  auto rows = Array();
  for (const auto& file : files)
    rows.array.push_back(Object(
        {{"name", String(file.name)}, {"bytes", Number(file.bytes.size())}}));
  return rows;
}
bool Valid(const std::vector<FilePackageEntry>& files) {
  if (files.size() > kSkillMaxFiles) return false;
  size_t total = 0;
  for (const auto& file : files) {
    if (!file_detail::ValidPackageMemberName(file.name, false) ||
        file.bytes.size() > kSkillFileBytes ||
        file.bytes.size() > file_detail::PackageLimits{}.max_bytes - total)
      return false;
    total += file.bytes.size();
  }
  return true;
}
bool ReadFiles(const Transfer& transfer, const Json* layout,
               std::vector<FilePackageEntry>* files) {
  if (!layout || !layout->IsArray() || layout->array.size() > kSkillMaxFiles)
    return false;
  size_t total = 0;
  for (const auto& row : layout->array) {
    auto name = row.GetString("name");
    const auto size = row.GetInt("bytes", -1);
    if (!file_detail::ValidPackageMemberName(name, false) || size < 0 ||
        static_cast<uint64_t>(size) > kSkillFileBytes ||
        static_cast<uint64_t>(size) >
            file_detail::PackageLimits{}.max_bytes - total)
      return false;
    total += size;
    auto& file = files->emplace_back(std::move(name), std::string(size, '\0'));
    if (size && !transfer(file.bytes.data(), size, false)) return false;
  }
  return true;
}
bool WriteFiles(const Transfer& transfer,
                std::vector<FilePackageEntry>& files) {
  for (auto& file : files)
    if (!file.bytes.empty() &&
        !transfer(file.bytes.data(), file.bytes.size(), true))
      return false;
  return true;
}
}  // namespace

ObjectReply ProcessSkillPackage(SkillPackage input, bool unpack,
                                const BinaryAccess& access,
                                SkillPackage* output) {
  if (!output || access.scope.empty())
    return Error(503, "object_scope_unavailable", "Skill owner required");
  const bool zip = !input.archive.empty();
  if (input.archive.empty() == input.files.empty())
    return Error(400, "invalid_skill_bundle",
                 "Supply one ZIP or a skill directory");
  if (input.archive.size() > kSkillArchiveBytes || !Valid(input.files))
    return Error(400, "invalid_skill_bundle",
                 "Invalid skill file names or sizes");
  if (access.cancelled && access.cancelled())
    return Failure(GrammarFailure::kCancelled);
  if (access.deadline <= Clock::now()) return Failure(GrammarFailure::kTimeout);
  GrammarControl control;
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.cancelled = access.cancelled;
  control.compile_deadline =
      std::min(access.deadline, Clock::now() + std::chrono::seconds(15));
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure cause;
  if (!detail::GrammarProcess::Start("--q4t-skill-worker", control, &process,
                                     &cause))
    return Failure(cause);
  Transfer transfer = [&](void* data, size_t size, bool write) {
    return process
        ->Transfer(data, size, write, control.compile_deadline,
                   access.cancelled)
        .ok();
  };
  auto request = Object({{"zip_bytes", Number(input.archive.size())},
                         {"files", Layout(input.files)},
                         {"unpack", Boolean(unpack)}});
  Json wire;
  uint8_t zip_input = zip;
  if (!transfer(&zip_input, sizeof(zip_input), true) ||
      !Metadata(transfer, true, 0, &request) ||
      (zip && !transfer(input.archive.data(), input.archive.size(), true)) ||
      !WriteFiles(transfer, input.files) ||
      !Metadata(transfer, false, 1, &wire))
    return Failure(process->Failure());
  auto parsed = DecodeReply(wire);
  if (!parsed.ok()) return parsed;
  auto& data = parsed.value;
  const auto size = data.GetInt("zip_bytes", -1);
  if (size <= 0 || static_cast<uint64_t>(size) > kSkillArchiveBytes ||
      (zip && static_cast<uint64_t>(size) != input.archive.size()))
    return Failure(GrammarFailure::kWorker);
  SkillPackage result;
  result.name = data.GetString("name");
  result.description = data.GetString("description");
  if (result.name.empty() || result.description.empty())
    return Failure(GrammarFailure::kWorker);
  if (zip)
    result.archive = std::move(input.archive);
  else {
    result.archive.resize(size);
    if (!transfer(result.archive.data(), result.archive.size(), false))
      return Failure(process->Failure());
  }
  if (!ReadFiles(transfer, data.Find("files"), &result.files) ||
      unpack != !result.files.empty())
    return Failure(process->Failure());
  *output = std::move(result);
  return {};
}

int MaybeRunSkillWorker(int argc, char** argv) {
  if (argc < 2 || std::string_view(argv[1]) != "--q4t-skill-worker") return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const Transfer transfer = detail::WorkerTransfer;
  try {
    uint8_t zip_input = 0;
    if (!transfer(&zip_input, sizeof(zip_input), false) || zip_input > 1)
      return 3;
    auto reply = LoadSkillRuntime(!zip_input);
    if (!detail::EnterNativeDocumentSandbox()) return 2;
    Json request;
    if (!Metadata(transfer, false, 0, &request)) return 3;
    const auto size = request.GetInt("zip_bytes", -1);
    const auto* members = request.GetArray("files");
    if (size < 0 || static_cast<uint64_t>(size) > kSkillArchiveBytes ||
        !members || members->array.size() > kSkillMaxFiles ||
        (size > 0) == !members->array.empty())
      return 3;
    const bool unpack = request.GetBool("unpack");
    if ((size > 0) != static_cast<bool>(zip_input)) return 3;
    BinaryAccess access{
        "skill-worker", Clock::now() + std::chrono::seconds(15), {}};
    Bundle bundle;
    if (size) {
      std::string bytes(size, '\0');
      if (!transfer(bytes.data(), bytes.size(), false)) return 3;
      if (reply.ok())
        reply = ReadSkillArchive(std::move(bytes), access, &bundle);
    } else {
      std::vector<FilePackageEntry> files;
      if (!ReadFiles(transfer, members, &files)) return 3;
      if (reply.ok())
        reply = MakeSkillArchive(std::move(files), access, &bundle);
    }
    if (!unpack) bundle.members.clear();
    if (reply.ok())
      reply.value =
          Object({{"name", String(bundle.manifest.name)},
                  {"description", String(bundle.manifest.description)},
                  {"zip_bytes", Number(bundle.archive.size())},
                  {"files", Layout(bundle.members)}});
    auto wire = WireReply(reply);
    if (!Metadata(transfer, true, 1, &wire)) return 3;
    if (reply.ok() && ((!size && !transfer(bundle.archive.data(),
                                           bundle.archive.size(), true)) ||
                       !WriteFiles(transfer, bundle.members)))
      return 3;
    return 0;
  } catch (...) {
    return 3;
  }
}
}  // namespace q4t::server
