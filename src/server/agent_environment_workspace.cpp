#include "agent_environment_internal.h"

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/file_input.h"
#include "q4t/server/request_limits.h"

namespace q4t::server::environment_detail {
using namespace object_detail;

ObjectReply SnapshotFile(ObjectStore& objects, const StateAccess& access,
                         const Json& source, FileInputBudget* budget,
                         Json* installed, Json* content) {
  FileInputSource input;
  const bool stored = source.GetString("type") == "file_id";
  const auto* value = source.Find(stored ? "file_id" : "data");
  if (!value || !value->IsString())
    return Error(400, "invalid_request", "Invalid environment file");
  input.kind =
      stored ? FileInputSource::Kind::kStored : FileInputSource::Kind::kInline;
  input.value = value->str;
  input.param = "files";
  FileInputContent file;
  auto result = ReadLocalFileInput(
      &objects, {access.scope, access.deadline, access.cancelled}, input,
      budget, &file);
  if (!result.ok()) return result;
  std::string encoded;
  if (stored && !io::EncodeBase64(file.bytes, kChatBodyBytes, &encoded))
    return Error(413, "environment_file_capacity",
                 "Environment file exceeds byte budget");
  auto id = NewObjectId(ObjectKind::kFile);
  if (id.empty())
    return Error(503, "environment_file_id_unavailable", "No file ID");
  auto metadata = Object({{"type", String(stored ? "file_id" : "inline")},
                          {"id", String(std::move(id))},
                          {"path", *source.Find("path")},
                          {"size_bytes", Number(file.bytes.size())}});
  if (stored) Set(&metadata, "file_id", *value);
  *installed = std::move(metadata);
  *content = Object({{"type", String("inline")},
                     {"path", *source.Find("path")},
                     {"data", stored ? String(std::move(encoded)) : *value}});
  return {};
}

ObjectReply PrepareWorkspace(ObjectStore& objects, const StateAccess& access,
                             const std::string& id, const Json& input,
                             Json* private_definition) {
  Json definition;
  auto result = ResolveDefinition(objects, access, input, &definition);
  if (!result.ok()) return result;
  // Record only equipment actually provisioned. Installing packages, starting
  // desktop services or running a setup command requires its execution owner.
  const auto nonempty = [](const Json& value, const char* field) {
    const auto* list = value.GetArray(field);
    return list && !list->array.empty();
  };
  const auto& settings = result.value;
  const auto& packages = *settings.Find("packages");
  if (settings.Find("desktop")->GetBool("enabled") ||
      nonempty(definition, "setup_commands") || nonempty(settings, "skills") ||
      nonempty(settings, "plugins") ||
      nonempty(settings, "capability_directories") ||
      nonempty(packages, "system") || nonempty(packages, "python") ||
      nonempty(packages, "npm") ||
      settings.Find("network")->GetString("access") != "disabled")
    return Error(
        503, "environment_executor_unavailable",
        "Requested environment features require a provisioned executor");
  auto metadata = Array(), snapshots = Array();
  FileInputBudget budget;
  budget.max_files = 50;
  if (const auto* files = definition.GetArray("files")) {
    for (const auto& file : files->array) {
      Json installed, content;
      auto copy =
          SnapshotFile(objects, access, file, &budget, &installed, &content);
      if (!copy.ok()) return copy;
      metadata.array.push_back(std::move(installed));
      snapshots.array.push_back(std::move(content));
    }
  }
  Set(&definition, "files", std::move(snapshots));
  Json resource = Object({{"id", String(id)},
                          {"object", String("agent.environment")},
                          {"type", String("openai_hosted")},
                          {"status", String("ready")},
                          {"files", std::move(metadata)},
                          {"plugins", Array()},
                          {"skills", Array()}});
  ChatPlanError error;
  if (!ValidateEnvironment(resource, EnvironmentShape::kResource, &error))
    return Error(503, "environment_record_invalid",
                 "Invalid installed environment metadata");
  *private_definition = std::move(definition);
  result.value = std::move(resource);
  return result;
}
}  // namespace q4t::server::environment_detail
