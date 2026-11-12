#include "q4t/server/prompt_registry.h"

#include <ctime>

#include "prompt_internal.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
using namespace object_detail;
namespace pd = prompt_detail;
namespace {
ObjectReply Corrupt() {
  return Error(503, "prompt_state_invalid", "Prompt record is inconsistent");
}
const Json* FindVersion(const Json& record, const std::string& version) {
  const auto* versions = record.GetArray("versions");
  if (!versions) return nullptr;
  for (const auto& value : versions->array)
    if (value.GetString("version") == version) return &value;
  return nullptr;
}
ObjectReply Conflict() {
  return Error(409, "prompt_revision_conflict", "Prompt revision changed");
}
Json VersionEntry(std::string version, const Json& definition) {
  return Object({{"version", String(std::move(version))},
                 {"created_at", Number(std::time(nullptr))},
                 {"definition", definition}});
}
Json InputItems(const Json* value) {
  if (!value || value->IsNull()) return Array();
  if (value->IsString())
    return Array({Object({{"role", String("user")}, {"content", *value}})});
  return *value;
}
}  // namespace

Json pd::Public(const Json& record) {
  const auto* data = record.Find("data");
  if (!data || !data->IsObject()) return {};
  auto result =
      Object({{"id", String(record.GetString("id"))},
              {"object", String("prompt")},
              {"created_at", Number(record.GetInt("created_at"))},
              {"revision", Number(record.GetInt("revision"))},
              {"default_version", String(data->GetString("default_version"))},
              {"versions", Array()}});
  const auto* versions = data->GetArray("versions");
  if (!versions) return {};
  for (const auto& version : versions->array)
    Field(result, "versions")
        .array.push_back(
            Object({{"version", String(version.GetString("version"))},
                    {"created_at", Number(version.GetInt("created_at"))}}));
  return result;
}

ObjectReply PromptRegistry::Read(const std::string& id,
                                 const StateAccess& access) {
  if (!ValidId(id)) return pd::Invalid("Invalid prompt ID", "prompt.id");
  ObjectRequest request;
  request.kind = ObjectKind::kPrompt;
  request.scope = access.scope;
  request.id = id;
  auto result = objects_.Execute(request, access.deadline, access.cancelled);
  if (!result.ok()) return result;
  const auto* data = result.value.Find("data");
  const auto* versions = data ? data->GetArray("versions") : nullptr;
  if (!data || data->GetInt("format") != 1 || !versions ||
      versions->array.empty() || versions->array.size() > pd::kVersions ||
      !FindVersion(*data, data->GetString("default_version")))
    return Corrupt();
  for (size_t i = 0; i < versions->array.size(); ++i) {
    const auto& version = versions->array[i];
    if (version.GetString("version") != std::to_string(i + 1) ||
        !version.Find("definition") || !version.Find("definition")->IsObject())
      return Corrupt();
  }
  return result;
}

ObjectReply PromptRegistry::Write(const std::string& id, int64_t revision,
                                  Json data, const StateAccess& access) {
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.kind = ObjectKind::kPrompt;
  request.id = id;
  request.scope = access.scope;
  request.expected_revision = revision;
  request.data = std::move(data);
  request.ttl_seconds = 0;
  auto result = objects_.Execute(request, access.deadline, access.cancelled);
  if (result.ok())
    result.value = pd::Public(result.value);
  else if (result.commit_unknown)
    result.value = Object({{"id", String(id)}});
  return result;
}

ObjectReply PromptRegistry::Create(const Json& definition,
                                   const ChatPlanConfig& config,
                                   const StateAccess& access) {
  auto valid = pd::ValidateDefinition(definition, config);
  if (!valid.ok()) return valid;
  const auto id = NewObjectId(ObjectKind::kPrompt);
  if (id.empty()) return Error(503, "prompt_id_unavailable", "No prompt ID");
  return Write(id, 0,
               Object({{"format", Number(1)},
                       {"default_version", String("1")},
                       {"versions", Array({VersionEntry("1", definition)})}}),
               access);
}

ObjectReply PromptRegistry::Append(const std::string& id, int64_t revision,
                                   const Json& definition,
                                   const ChatPlanConfig& config,
                                   const StateAccess& access) {
  auto saved = Read(id, access);
  if (!saved.ok()) return saved;
  if (revision <= 0 || saved.value.GetInt("revision") != revision)
    return Conflict();
  auto valid = pd::ValidateDefinition(definition, config);
  if (!valid.ok()) return valid;
  auto data = *saved.value.Find("data");
  auto& versions = Field(data, "versions");
  if (versions.array.size() >= pd::kVersions)
    return Error(409, "prompt_version_capacity",
                 "Prompt version limit reached");
  const auto version = std::to_string(versions.array.size() + 1);
  versions.array.push_back(VersionEntry(version, definition));
  Set(&data, "default_version", String(version));
  return Write(id, revision, std::move(data), access);
}

ObjectReply PromptRegistry::Select(const std::string& id, int64_t revision,
                                   const std::string& version,
                                   const StateAccess& access) {
  if (!pd::Version(version))
    return pd::Invalid("Invalid prompt version", "version");
  auto saved = Read(id, access);
  if (!saved.ok()) return saved;
  if (revision <= 0 || saved.value.GetInt("revision") != revision)
    return Conflict();
  auto data = *saved.value.Find("data");
  if (!FindVersion(data, version))
    return Error(404, "prompt_version_not_found", "Prompt version not found");
  if (data.GetString("default_version") == version) {
    saved.value = pd::Public(saved.value);
    return saved;
  }
  Set(&data, "default_version", String(version));
  return Write(id, revision, std::move(data), access);
}

ObjectReply PromptRegistry::Retrieve(const std::string& id,
                                     const std::string& version,
                                     const StateAccess& access) {
  auto saved = Read(id, access);
  if (!saved.ok()) return saved;
  const auto data = *saved.value.Find("data");
  auto value = pd::Public(saved.value);
  if (!version.empty()) {
    const auto* found = FindVersion(data, version);
    if (!found)
      return Error(404, "prompt_version_not_found", "Prompt version not found");
    value = *found;
    Set(&value, "id", String(id));
    Set(&value, "object", String("prompt.version"));
  }
  saved.value = std::move(value);
  return saved;
}

ObjectReply PromptRegistry::Erase(const std::string& id,
                                  const StateAccess& access) {
  auto saved = Read(id, access);
  if (!saved.ok()) return saved;
  ObjectRequest request;
  request.operation = ObjectOperation::kErase;
  request.kind = ObjectKind::kPrompt;
  request.id = id;
  request.scope = access.scope;
  request.expected_revision = saved.value.GetInt("revision");
  auto result = objects_.Execute(request, access.deadline, access.cancelled);
  if (result.ok())
    result.value = Object({{"id", String(id)},
                           {"object", String("prompt.deleted")},
                           {"deleted", Boolean(true)}});
  else if (result.commit_unknown)
    result.value = Object({{"id", String(id)}});
  return result;
}

ObjectReply PromptRegistry::List(ObjectRequest request,
                                 const StateAccess& access) {
  request.operation = ObjectOperation::kList;
  request.kind = ObjectKind::kPrompt;
  request.scope = access.scope;
  auto result = objects_.Execute(request, access.deadline, access.cancelled);
  if (result.ok()) {
    auto& records = Field(result.value, "data");
    if (!records.IsArray()) return Corrupt();
    for (auto& record : records.array) {
      record = pd::Public(record);
      if (record.IsNull()) return Corrupt();
    }
  }
  return result;
}

ObjectReply PromptRegistry::Expand(const Json& request,
                                   const ChatPlanConfig& config,
                                   const StateAccess& access) {
  const auto* reference = request.Find("prompt");
  std::string bad;
  if (!reference ||
      !InputFields(*reference, {"id", "version", "variables"}, &bad, "prompt"))
    return pd::Invalid("Invalid prompt reference",
                       bad.empty() ? "prompt" : bad);
  const auto* identity = reference->Find("id");
  const auto* version = reference->Find("version");
  if (!identity || !identity->IsString() || !ValidId(identity->str))
    return pd::Invalid("Invalid prompt ID", "prompt.id");
  if (version && !version->IsNull() &&
      (!version->IsString() || !pd::Version(version->str)))
    return pd::Invalid("Invalid prompt version", "prompt.version");
  auto stored = Read(identity->str, access);
  if (!stored.ok()) return stored;
  const auto& data = *stored.value.Find("data");
  const auto selected = version && version->IsString()
                            ? version->str
                            : data.GetString("default_version");
  const auto* found = FindVersion(data, selected);
  if (!found)
    return Error(404, "prompt_version_not_found", "Prompt version not found");
  const auto* variables = reference->Find("variables");
  auto result =
      pd::Render(*found->Find("definition"),
                 variables && !variables->IsNull() ? *variables : Object());
  if (!result.ok()) return result;
  const auto template_input = InputItems(result.value.Find("input"));
  auto current_input = InputItems(request.Find("input"));
  if (!current_input.IsArray()) return pd::Invalid("Invalid input", "input");
  if (template_input.array.size() + current_input.array.size() > 4096)
    return Error(400, "input_limit_exceeded",
                 "Expanded input exceeds 4096 items");
  current_input.array.insert(current_input.array.begin(),
                             template_input.array.begin(),
                             template_input.array.end());
  // Explicit request fields win, including explicit null. No hidden template
  // values can select store/background, identity, deadlines or cancellation.
  for (const auto& [name, value] : request.object)
    Set(&result.value, name, value);
  Set(&result.value, "input", std::move(current_input));
  auto pinned = *reference;
  Set(&pinned, "version", String(selected));
  Set(&result.value, "prompt", std::move(pinned));
  if (OutputJson(result.value).size() > pd::kExpandedBytes)
    return Error(413, "prompt_expansion_limit",
                 "Expanded request is too large");
  ResponsesEnvelope envelope;
  ChatPlanError error;
  const auto status =
      ParseResponsesEnvelope(result.value, config, &envelope, &error);
  if (!status) {
    auto failure = Error(error.http_status, error.code, status.message());
    failure.value = Object({{"param", String(error.param)}});
    return failure;
  }
  return result;
}
}  // namespace q4t::server
