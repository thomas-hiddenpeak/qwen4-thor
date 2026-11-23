#include "agent_environment_internal.h"

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/file_input.h"
#include "q4t/server/request_limits.h"

namespace q4t::server::environment_detail {
using namespace object_detail;

ObjectReply SnapshotFile(const Json& source, WorkspaceCopies& copies,
                         std::string param, Json* installed, Json* content) {
  const bool stored = source.GetString("type") == "file_id";
  const auto id = NewObjectId(ObjectKind::kFile);
  if (id.empty())
    return Error(503, "environment_file_id_unavailable", "No file ID");
  uint64_t bytes = 0;
  if (stored) {
    auto result =
        copies.CopyFile(source, id, std::move(param), &bytes, content);
    if (!result.ok()) return result;
  } else {
    const auto* data = source.Find("data");
    size_t count = 0;
    if (!data || !data->IsString() || !io::Base64DecodedSize(data->str, &count))
      return Error(400, "invalid_request", "Invalid inline environment file");
    bytes = count;
    *content = source;
  }
  *installed = Object({{"type", String(stored ? "file_id" : "inline")},
                       {"id", String(id)},
                       {"path", *source.Find("path")},
                       {"size_bytes", Number(bytes)}});
  if (stored) Set(installed, "file_id", *source.Find("file_id"));
  return {};
}

ObjectReply PrepareWorkspace(ObjectStore& objects, const StateAccess& access,
                             const std::string& id, const Json& input,
                             Json* private_definition,
                             WorkspaceCopies& copies) {
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
  if (const auto* files = definition.GetArray("files")) {
    if (files->array.size() > 50) {
      auto failure = Error(400, "invalid_request", "Expected at most 50 workspace files");
      failure.value = Object({{"param",String("environment.files")}});
      return failure;
    }
    for (const auto& file : files->array) {
      Json installed, content;
      auto copy =
          SnapshotFile(file, copies,
                       "environment.files[" +
                           std::to_string(metadata.array.size()) + "].file_id",
                       &installed, &content);
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
