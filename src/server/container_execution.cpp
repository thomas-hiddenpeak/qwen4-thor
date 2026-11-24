#include "containers_internal.h"

#include <algorithm>
#include <ctime>
#include <set>
#include <unordered_map>

#include "file_package.h"
#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/file_input.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/program_worker.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using container_detail::Stored;
constexpr size_t kFile = 8ULL << 20;
constexpr size_t kWorkspace = 16ULL << 20;
constexpr size_t kEntries = 256;
constexpr std::string_view kData = "/mnt/data/";

ObjectReply Capacity() {
  return Error(413, "code_interpreter_workspace_capacity",
               "Interpreter workspace exceeds the configured tool limits");
}
ObjectReply InvalidWorker() {
  return Error(503, "code_interpreter_worker_invalid",
               "Invalid interpreter result");
}
struct Snapshot {
  Json files = Array();
  std::set<std::string> paths;
  std::unordered_map<std::string, std::string> manifests;
  size_t bytes = 0;

  ObjectReply Add(std::string path, std::string_view data, bool writable) {
    if (data.size() > kFile || data.size() > kWorkspace - bytes ||
        files.array.size() >= kEntries)
      return Capacity();
    if (!paths.insert(path).second) return container_detail::Corrupt();
    std::string encoded;
    if (!io::EncodeBase64(data, 2 * kFile, &encoded)) return Capacity();
    bytes += data.size();
    files.array.push_back(Object({{"path", String(std::move(path))},
                                  {"data", String(std::move(encoded))},
                                  {"writable", Boolean(writable)}}));
    return {};
  }
};

ObjectReply LoadFiles(ObjectStore& objects, const StateAccess& access,
                      const Stored& state, Snapshot* snapshot) {
  const BinaryAccess owner{access.scope, access.deadline, access.cancelled};
  BinaryStore binary(objects);
  for (const auto& file : state.workspace.data.GetArray("files")->array) {
    ChatPlanError error;
    if (!ValidateContainer(file, ContainerShape::kFileCreated, &error) ||
        file.GetString("container_id") != state.metadata.id)
      return container_detail::Corrupt();
    const auto size = file.GetInt("bytes", -1);
    if (size < 0 || static_cast<uint64_t>(size) > kFile ||
        static_cast<uint64_t>(size) > kWorkspace - snapshot->bytes)
      return Capacity();
    std::vector<BinaryRead> pins(1);
    ScopeExit close(
        [&] { container_detail::CloseFiles(objects, access, &pins); });
    auto& pin = pins.front();
    Json data;
    auto opened = binary.Open(owner, ObjectKind::kContainerFile,
                              file.GetString("id"), 60, &pin, &data);
    if (!opened.ok()) return opened;
    if (!data.Find("resource") ||
        OutputJson(*data.Find("resource")) != OutputJson(file) ||
        pin.manifest.bytes != static_cast<uint64_t>(size))
      return container_detail::Corrupt();
    snapshot->manifests[file.GetString("id")] =
        Digest(OutputJson(*data.Find("blob")));
    std::string bytes;
    bytes.reserve(size);
    for (size_t i = 0; i < pin.manifest.chunks.size(); ++i) {
      std::string part;
      auto read = binary.Read(owner, pin, i, &part);
      if (!read.ok()) return read;
      bytes += part;
    }
    auto added = snapshot->Add(file.GetString("path"), bytes, true);
    if (!added.ok()) return added;
  }
  return {};
}

ObjectReply WriteDelta(ObjectStore& objects, const StateAccess& access,
                       Stored* state, const Json& change, std::string path,
                       std::vector<BinaryStage>* stages,
                       std::vector<ObjectRequest>* writes, Json* output) {
  auto& entries = Field(state->workspace.data, "files").array;
  auto found = std::ranges::find_if(
      entries, [&](const Json& f) { return f.GetString("path") == path; });
  ObjectRequest file;
  file.scope = access.scope;
  file.kind = ObjectKind::kContainerFile;
  file.ttl_seconds = 0;
  const bool exists = found != entries.end();
  if (exists) {
    file.id = found->GetString("id");
    auto current = objects.Execute(file, access.deadline, access.cancelled);
    if (!current.ok()) return current;
    const auto* data = current.value.Find("data");
    if (!data || !data->Find("resource") ||
        OutputJson(*data->Find("resource")) != OutputJson(*found))
      return Error(409, "object_revision_conflict", "Container file changed");
    file.expected_revision = current.value.GetInt("revision");
    file.created_at = current.value.GetInt("created_at");
  }
  if (const auto* removed = change.Find("deleted")) {
    if (!exists || !removed->IsBool() || !removed->boolean)
      return InvalidWorker();
    file.operation = ObjectOperation::kErase;
    writes->push_back(std::move(file));
    entries.erase(found);
    return {};
  }
  const auto* encoded = change.Find("data");
  if (!encoded || !encoded->IsString()) return InvalidWorker();
  const BinaryAccess owner{access.scope, access.deadline, access.cancelled};
  FileInputContent content;
  if (!encoded->str.empty()) {
    FileInputBudget budget;
    budget.max_bytes = kFile;
    FileInputSource source;
    source.value = encoded->str;
    source.filename = "output.bin";
    source.mime_type = "application/octet-stream";
    source.param = "code_interpreter";
    auto decoded =
        ReadLocalFileInput(nullptr, owner, source, &budget, &content);
    if (!decoded.ok()) return decoded;
  }
  if (!exists) {
    file.id = NewObjectId(file.kind);
    if (file.id.empty())
      return Error(503, "object_entropy_unavailable", "No file ID");
  }
  Json resource = exists ? *found
                         : Object({{"object", String("container.file")},
                                   {"id", String(file.id)},
                                   {"container_id", String(state->metadata.id)},
                                   {"created_at", Number(std::time(nullptr))},
                                   {"path", String(std::move(path))},
                                   {"source", String("assistant")}});
  Set(&resource, "bytes", Number(content.bytes.size()));
  ChatPlanError error;
  if (!ValidateContainer(resource, ContainerShape::kFileCreated, &error))
    return InvalidWorker();
  file.operation = ObjectOperation::kPut;
  file.data = Object(
      {{"format", String("q4t.container_file.v1")}, {"resource", resource}});
  auto& stage = stages->emplace_back();
  BinaryStore binary(objects);
  auto created = binary.Begin(owner, kFile, 60, &stage);
  if (!created.ok()) return created;
  std::string_view rest(content.bytes);
  while (!rest.empty()) {
    auto part = rest.substr(0, kBinaryChunkBytes);
    auto appended = binary.Append(owner, &stage, part);
    if (!appended.ok()) return appended;
    rest.remove_prefix(part.size());
  }
  auto prepared = binary.PreparePublish(owner, stage, std::move(file), writes);
  if (!prepared.ok()) return prepared;
  if (exists)
    *found = resource;
  else
    entries.push_back(resource);
  output->array.push_back(std::move(resource));
  return {};
}
}  // namespace

ObjectReply ExecuteContainerPython(ObjectStore& objects,
                                   const StateAccess& access,
                                   std::string_view container,
                                   std::string_view code) {
  if (code.empty() || code.size() > (64 << 10))
    return Error(400, "code_interpreter_code_invalid",
                 "Expected a bounded Python program");
  std::vector<FilePackageEntry> skills;
  auto opened = ReadContainerSkillFiles(objects, access, container, &skills);
  if (!opened.ok()) return opened;
  Stored state;
  opened = container_detail::Read(objects, access, container, &state);
  if (opened.ok())
    opened = container_detail::OpenWorkspace(objects, access, &state);
  if (!opened.ok()) return opened;
  Snapshot snapshot;
  for (const auto& item : skills) {
    auto added = snapshot.Add("/mnt/skills/" + item.name, item.bytes, false);
    if (!added.ok()) return added;
  }
  const auto skill_bytes = snapshot.bytes;
  const auto skill_files = snapshot.paths.size();
  auto loaded = LoadFiles(objects, access, state, &snapshot);
  if (!loaded.ok()) return loaded;
  Json request = Object({{"code", String(std::string(code))},
                         {"files", std::move(snapshot.files)}});
  auto run = ExecutePythonWorker(
      request, {access.scope, access.deadline, access.cancelled});
  if (!run.ok()) return run;
  const auto& changes = run.value.GetArray("files")->array;
  if (changes.size() > kEntries ||
      run.value.GetString("logs").size() > (68 << 10))
    return InvalidWorker();
  std::set<std::string> paths;
  std::vector<ObjectRequest> writes;
  std::vector<BinaryStage> stages;
  ScopeExit cleanup(
      [&] { container_detail::FinishSkillStages(objects, access, &stages); });
  Json output = Array();
  for (const auto& change : changes) {
    auto path = change.GetString("path");
    if (!change.IsObject() || change.object.size() != 2 ||
        !path.starts_with(kData) ||
        !file_detail::ValidPackageMemberName(
            std::string_view(path).substr(kData.size()), false) ||
        !paths.insert(path).second)
      return InvalidWorker();
    auto staged = WriteDelta(objects, access, &state, change, std::move(path),
                             &stages, &writes, &output);
    if (!staged.ok()) return staged;
  }
  // The worker is an untrusted producer. Recount the committed workspace even
  // when a consumer deliberately returns a forged or oversized file delta.
  size_t total = skill_bytes;
  const auto& entries = state.workspace.data.GetArray("files")->array;
  if (entries.size() > kEntries - skill_files) return Capacity();
  for (const auto& entry : entries) {
    const auto bytes = entry.GetInt("bytes", -1);
    if (bytes < 0 || static_cast<uint64_t>(bytes) > kWorkspace - total)
      return Capacity();
    total += static_cast<size_t>(bytes);
  }
  auto generated = Array();
  for (const auto& write : writes)
    if (write.kind == ObjectKind::kContainerFile &&
        write.operation == ObjectOperation::kPut)
      snapshot.manifests[write.id] =
          Digest(OutputJson(*write.data.Find("blob")));
  for (const auto& entry : entries) {
    if (entry.GetString("source") != "assistant") continue;
    auto copy = entry;
    Set(&copy, "blob_sha256",
        String(snapshot.manifests.at(entry.GetString("id"))));
    generated.array.push_back(std::move(copy));
  }
  auto committed = container_detail::Commit(objects, access, std::move(state),
                                            std::move(writes));
  for (auto& stage : stages) {
    stage.closed = committed.ok();
    stage.commit_unknown = stage.commit_unknown || committed.commit_unknown;
  }
  if (!committed.ok()) return committed;
  Set(&run.value, "files", std::move(output));
  Set(&run.value, "generated_files", std::move(generated));
  Set(&run.value, "container_id", String(std::string(container)));
  return run;
}

std::string AssistantOutputFileId(const std::string& message,
                                  const std::string& container_file) {
  return "file_" + Digest(message + "/" + container_file).substr(0, 32);
}

ObjectReply ExportAssistantOutputFile(ObjectStore& objects,
                                      const StateAccess& access,
                                      const std::string& message,
                                      const Json& file) {
  const auto container = file.GetString("container_id");
  const auto id = file.GetString("id");
  if (!ValidId(message) || !ValidId(container) || !ValidId(id) ||
      file.GetString("source") != "assistant")
    return container_detail::Corrupt();
  const BinaryAccess owner{access.scope, access.deadline, access.cancelled};
  BinaryStore binary(objects);
  std::vector<BinaryRead> pins(1);
  ScopeExit close(
      [&] { container_detail::CloseFiles(objects, access, &pins); });
  Json stored;
  auto pinned = binary.Open(owner, ObjectKind::kContainerFile, id, 60,
                            &pins.front(), &stored);
  if (!pinned.ok()) return pinned;
  const auto* original = stored.Find("resource");
  if (!original || original->GetString("id") != id ||
      original->GetString("container_id") != container ||
      original->GetString("path") != file.GetString("path") ||
      original->GetString("source") != "assistant" ||
      file.GetString("blob_sha256") != Digest(OutputJson(*stored.Find("blob"))))
    return Error(409, "container_file_changed",
                 "Generated file changed before citation");
  ObjectRequest write;
  write.kind = ObjectKind::kFile;
  write.scope = access.scope;
  write.id = AssistantOutputFileId(message, id);
  write.ttl_seconds = 0;
  write.operation = ObjectOperation::kPut;
  write.created_at = std::time(nullptr);
  const auto path = file.GetString("path");
  const auto resource =
      Object({{"id", String(write.id)},
              {"object", String("file")},
              {"bytes", *file.Find("bytes")},
              {"created_at", Number(write.created_at)},
              {"filename", String(path.substr(path.rfind('/') + 1))},
              {"purpose", String("assistants_output")},
              {"status", String("processed")}});
  write.data = Object({{"format", String("q4t.file.v1")},
                       {"file", resource},
                       {"mime_type", String("application/octet-stream")},
                       {"blob", BinaryManifestJson(pins.front().manifest)}});
  return objects.Execute(write, access.deadline, access.cancelled);
}
}  // namespace q4t::server
