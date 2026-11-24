#include "containers_internal.h"

#include <algorithm>
#include <set>

#include "object_protocol.h"
#include "q4t/server/file_input.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/skills.h"
#include "q4t/server/request_json.h"
#include "q4t/server/state_cipher.h"

namespace q4t::server::container_detail {
namespace {
using namespace object_detail;
BinaryAccess Source(const StateAccess& access) {
  return {access.scope, access.deadline, access.cancelled};
}
ObjectReply InlinePackage(const Json& spec, const BinaryAccess& access,
                          FileInputBudget* budget, SkillPackage* package) {
  const auto* source = spec.Find("source");
  if (!source || source->GetString("type") != "base64" ||
      source->GetString("media_type") != "application/zip" ||
      !source->Find("data") || !source->Find("data")->IsString())
    return Invalid("Expected a base64 ZIP", "skills.source");
  FileInputSource input;
  input.param = "skills.source.data";
  input.value = source->Find("data")->str;
  input.filename = "skill.zip";
  input.mime_type = "application/zip";
  FileInputContent decoded;
  auto ready = ReadLocalFileInput(nullptr, access, input, budget, &decoded);
  if (!ready.ok()) return ready;
  SkillPackage bundle;
  bundle.archive = std::move(decoded.bytes);
  return ProcessSkillPackage(std::move(bundle), false, access, package);
}
}  // namespace

void FinishSkillStages(ObjectStore& objects, const StateAccess& access,
                       std::vector<BinaryStage>* stages) {
  const BinaryAccess cleanup{
      access.scope,
      std::chrono::steady_clock::now() + std::chrono::seconds(3),
      {}};
  for (auto& stage : *stages) {
    if (stage.id.empty() || stage.closed || stage.commit_unknown) continue;
    try {
      if (!BinaryStore(objects).Abort(cleanup, &stage).ok())
        objects.RecordAuxiliaryCleanupFailure();
    } catch (...) {
      objects.RecordAuxiliaryCleanupFailure();
    }
  }
}

ObjectReply PrepareSkills(ObjectStore& objects, const StateAccess& access,
                          const Json& skills, Stored* state,
                          std::vector<BinaryRead>* pins,
                          std::vector<BinaryStage>* stages,
                          std::vector<ObjectRequest>* writes) {
  if (skills.array.empty()) return {};
  auto mounts = Array();
  std::set<std::string> directories;
  BinaryStore blobs(objects);
  FileInputBudget budget;
  // Each inline package needs a root and a staging removal plus the two
  // workspace records shared by every mount.
  budget.max_files = (kMaxTransactionObjects - 2) / 2;
  const auto owner = Source(access);
  for (const auto& spec : skills.array) {
    const bool ref = spec.GetString("type") == "skill_reference";
    const size_t mutations = ref ? 1 : 2;
    if (writes->size() + mutations + 2 > kMaxTransactionObjects)
      return Invalid("Skill attachments exceed workspace capacity", "skills");
    ObjectRequest root;
    root.scope = access.scope;
    root.kind = ObjectKind::kToolRun;
    root.operation = ObjectOperation::kPut;
    root.ttl_seconds = 0;
    auto id = NewObjectId(root.kind);
    if (id.empty())
      return Error(503, "object_entropy_unavailable", "No skill mount ID");
    root.id = "mount_" + id;
    Json mount = Object({{"id", String(root.id)}});
    std::string name, description;
    BinaryStage* stage = nullptr;
    if (ref) {
      const auto label = spec.GetString("version");
      if (spec.Has("version") && label != "latest" &&
          !ValidSkillVersionNumber(label))
        return Invalid("Invalid skill version selector", "skills.version");
      auto& pin = pins->emplace_back();
      auto opened = OpenSkillVersion(objects, access,
                                     spec.GetString("skill_id"), label, &pin);
      if (!opened.ok()) return opened;
      name = opened.value.GetString("name");
      description = opened.value.GetString("description");
      Set(&mount, "skill_id", *opened.value.Find("skill_id"));
      Set(&mount, "version", *opened.value.Find("version"));
      root.data = Object({{"blob", BinaryManifestJson(pin.manifest)}});
    } else {
      SkillPackage bundle;
      auto imported = InlinePackage(spec, owner, &budget, &bundle);
      if (!imported.ok()) return imported;
      // The validated archive determines paths. Per-attachment discovery
      // metadata can differ from the manifest without becoming a host path.
      name = bundle.name;
      description = spec.GetString("description");
      Set(&mount, "display_name", *spec.Find("name"));
      stage = &stages->emplace_back();
      auto begin = blobs.Begin(owner, kFileInputBytes, 3600, stage);
      if (!begin.ok()) return begin;
      for (std::string_view bytes(bundle.archive); !bytes.empty();) {
        const auto part = bytes.substr(0, kBinaryChunkBytes);
        auto stored = blobs.Append(owner, stage, part);
        if (!stored.ok()) return stored;
        bytes.remove_prefix(part.size());
      }
      root.data = Object();
    }
    std::string directory = name;
    if (!directories.insert(directory).second) {
      directory += "-" + root.id;
      directories.insert(directory);
    }
    Set(&mount, "directory", String(directory));
    Set(&mount, "name", String(name));
    Set(&mount, "description", String(description));
    Set(&root.data, "format", String("q4t.container_skill.v1"));
    Set(&root.data, "container_id", String(state->metadata.id));
    Set(&root.data, "mount", mount);
    if (stage) {
      auto prepared =
          blobs.PreparePublish(owner, *stage, std::move(root), writes);
      if (!prepared.ok()) return prepared;
    } else {
      writes->push_back(std::move(root));
    }
    mounts.array.push_back(std::move(mount));
  }
  Set(&state->workspace.data, "mounted_skills", std::move(mounts));
  return {};
}

// Earlier workspaces sealed attachment specifications before the registry
// could mount them. Resolve those specifications once under the workspace CAS.
ObjectReply ResolveLegacyMounts(ObjectStore& objects, const StateAccess& access,
                                Stored* state) {
  auto& data = state->workspace.data;
  if (data.Has("mounted_skills") || !data.Has("execution_options")) return {};
  const auto& id = state->metadata.id;
  const StateCipherBinding binding{"q4t-containers", id, "execution-options",
                                   id};
  StateCipher cipher(objects);
  std::string plaintext;
  auto opened = cipher.Open(data.GetString("execution_options"), binding,
                            access, &plaintext);
  if (!opened.ok()) return opened;
  Json options;
  if (!io::ParseJson(plaintext, &options, true, kApplicationJsonLimits) ||
      !options.IsObject())
    return Corrupt();
  const auto* skills = options.Find("skills");
  if (!skills) return {};
  ChatPlanError error;
  auto spec = Object({{"name", String("legacy")}, {"skills", *skills}});
  if (!skills->IsArray() ||
      !ValidateContainer(spec, ContainerShape::kCreate, &error))
    return Corrupt();
  std::vector<BinaryRead> pins;
  std::vector<BinaryStage> stages;
  std::vector<ObjectRequest> writes;
  ScopeExit cleanup([&] {
    CloseFiles(objects, access, &pins);
    FinishSkillStages(objects, access, &stages);
  });
  Set(&data, "mounted_skills", Array());
  auto prepared =
      PrepareSkills(objects, access, *skills, state, &pins, &stages, &writes);
  if (!prepared.ok()) return prepared;
  std::erase_if(options.object,
                [](const auto& item) { return item.first == "skills"; });
  if (options.object.empty()) {
    std::erase_if(data.object, [](const auto& item) {
      return item.first == "execution_options";
    });
  } else {
    std::string sealed;
    auto replaced = cipher.Seal(OutputJson(options), binding, access, &sealed);
    if (!replaced.ok()) return replaced;
    Set(&data, "execution_options", String(std::move(sealed)));
  }
  auto result = Commit(objects, access, *state, std::move(writes));
  for (auto& stage : stages) {
    stage.closed = result.ok();
    stage.commit_unknown = result.commit_unknown;
  }
  return result;
}

ObjectReply ReadMount(ObjectStore& objects, const StateAccess& access,
                      const std::string& container, const Json& mount,
                      size_t* remaining, std::vector<FilePackageEntry>* files) {
  const auto id = mount.GetString("id");
  const auto directory = mount.GetString("directory");
  if (!id.starts_with("mount_") || !ValidId(id) || directory.empty() ||
      directory == "." || directory == ".." ||
      directory.find_first_of("/\\") != directory.npos)
    return Corrupt();
  std::vector<BinaryRead> pins(1);
  ScopeExit cleanup([&] { CloseFiles(objects, access, &pins); });
  auto& pin = pins.front();
  BinaryStore blobs(objects);
  const auto owner = Source(access);
  Json data;
  auto opened = blobs.Open(owner, ObjectKind::kToolRun, id, 3600, &pin, &data);
  if (!opened.ok()) return opened;
  if (data.GetString("format") != "q4t.container_skill.v1" ||
      data.GetString("container_id") != container || !data.Find("mount") ||
      OutputJson(*data.Find("mount")) != OutputJson(mount) ||
      !pin.manifest.bytes || pin.manifest.bytes > kFileInputBytes)
    return Corrupt();
  SkillPackage packed;
  packed.archive.reserve(pin.manifest.bytes);
  for (size_t i = 0; i < pin.manifest.chunks.size(); ++i) {
    std::string part;
    auto read = blobs.Read(owner, pin, i, &part);
    if (!read.ok()) return read;
    packed.archive.append(part);
  }
  SkillPackage expanded;
  auto decoded = ProcessSkillPackage(std::move(packed), true, owner, &expanded);
  if (!decoded.ok()) return decoded;
  if (expanded.name != mount.GetString("name")) return Corrupt();
  for (auto& file : expanded.files) {
    if (file.bytes.size() > *remaining)
      return Error(413, "skill_mount_capacity",
                   "Skill files exceed workspace read capacity");
    *remaining -= file.bytes.size();
    file.name.replace(0, expanded.name.size(), directory);
    files->push_back(std::move(file));
  }
  return {};
}
}  // namespace q4t::server::container_detail

namespace q4t::server {
ObjectReply ReadContainerSkillFiles(ObjectStore& objects,
                                    const StateAccess& access,
                                    std::string_view container,
                                    std::vector<FilePackageEntry>* files) {
  using namespace container_detail;
  if (!files) return Corrupt();
  Stored state;
  for (;;) {
    state = {};
    auto metadata = Read(objects, access, container, &state);
    if (!metadata.ok()) return metadata;
    auto opened = OpenWorkspace(objects, access, &state);
    if (opened.ok()) opened = ResolveLegacyMounts(objects, access, &state);
    if (opened.ok()) break;
    if (opened.code != "object_revision_conflict" || opened.commit_unknown ||
        std::chrono::steady_clock::now() >= access.deadline ||
        (access.cancelled && access.cancelled()))
      return opened;
  }
  std::vector<FilePackageEntry> selected;
  size_t remaining = FilePackageLimits{}.max_bytes;
  if (const auto* mounts = state.workspace.data.Find("mounted_skills")) {
    if (!mounts->IsArray()) return Corrupt();
    for (const auto& mount : mounts->array) {
      auto read = ReadMount(objects, access, state.metadata.id, mount,
                            &remaining, &selected);
      if (!read.ok()) return read;
    }
  }
  *files = std::move(selected);
  return {};
}
}  // namespace q4t::server
