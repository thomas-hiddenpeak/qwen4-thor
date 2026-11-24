#include "q4t/server/responses_item_shape.h"

#include <algorithm>
#include <unordered_set>

#include "q4t/io/base64.h"
#include "q4t/server/decision_plan.h"
#include "q4t/server/decision_types_data.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/live_shape.h"
#include "q4t/server/live_types_data.h"
#include "q4t/server/multi_agent_types_data.h"
#include "q4t/server/agent_session.h"
#include "q4t/server/agent_environment.h"
#include "q4t/server/environment_types_data.h"
#include "q4t/server/agent_session_types_data.h"
#include "q4t/server/agent_registration.h"
#include "q4t/server/vault_credential_types_data.h"
#include "vault_credential_internal.h"
#include "q4t/server/assistant_registry.h"
#include "q4t/server/assistant_types_data.h"
#include "q4t/server/agent_registration_types_data.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/responses_item_types_data.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/server/realtime_types_data.h"

namespace q4t::server {
namespace {
using io::Json;
struct Graph {
  Json value;
  Status status;
  Graph(std::string_view data, std::string_view profile) {
    io::JsonParseLimits limits;
    limits.max_values = 100000;
    limits.max_string_bytes = 1024 * 1024;
    limits.reject_duplicate_keys = true;
    status =
        io::ParseJson(std::string(data), &value, false, limits);
    const auto* nodes = value.GetArray("nodes");
    if (status &&
        (value.GetString("profile") != profile || !nodes ||
         nodes->array.empty() || nodes->array.size() > 4096 ||
         !value.Find("roots") || !value.Find("roots")->IsObject()))
      status = Status::Fail("invalid compiled item contract");
  }
};
const Graph& Types() {
  static const Graph graph(item_types::kData, "q4t-openai-item-types-v1");
  return graph;
}
const Graph& RealtimeTypes() {
  static const Graph graph(realtime_types::kData,
                            "q4t-openai-realtime-types-v1");
  return graph;
}
const Graph& EnvironmentTypes() {
  static const Graph graph(environment_types::kData,
                           "q4t-openai-environment-types-v1");
  return graph;
}
const Graph& LiveTypes() {
  static const Graph graph(live_types::kData, "q4t-openai-live-types-v1");
  return graph;
}
std::string FieldPath(std::string_view path, std::string_view name) {
  // A rejected arbitrary key cannot expand an error response without bound.
  if (name.size() > 128) return std::string(path) + "[<long_key>]";
  return InputParamPath(path, name);
}
struct Failure {
  std::string path;
  std::string message;
  bool resource = false;
  bool internal = false;
};
class Validator {
 public:
  Validator(const Json& nodes, bool field_bounds)
      : nodes_(nodes.array), field_bounds_(field_bounds) {}
  bool Match(int64_t index, const Json& value, const std::string& path,
             size_t depth, Failure* failure) {
    if (++work_ > 2 * 1024 * 1024 || depth > 64)
      return Fail(path, "Item validation resource limit exceeded", failure,
                  true);
    if (index < 0 || static_cast<size_t>(index) >= nodes_.size())
      return Internal(path, failure);
    const auto& node = nodes_[index];
    const auto kind = node.GetString("kind");
    if (kind == "any") return true;
    if (kind == "never")
      return Fail(path, "Value is not a JSON data type", failure);
    if (kind == "null")
      return value.IsNull() || Fail(path, "Expected null", failure);
    if (kind == "string")
      return value.IsString() || Fail(path, "Expected text", failure);
    if (kind == "number")
      return value.IsNumber() || Fail(path, "Expected number", failure);
    if (kind == "boolean")
      return value.IsBool() || Fail(path, "Expected boolean", failure);
    if (kind == "literal") {
      const auto* expected = node.Find("value");
      if (!expected) return Internal(path, failure);
      const bool same =
          expected->type == value.type &&
          ((value.IsString() && expected->str == value.str) ||
           (value.IsNumber() && expected->number == value.number) ||
           (value.IsBool() && expected->boolean == value.boolean) ||
           value.IsNull());
      return same || Fail(path, "Value is outside the item enum", failure);
    }
    if (kind == "union") {
      const auto* variants = node.GetArray("variants");
      if (!variants || variants->array.empty()) return Internal(path, failure);
      Failure best;
      for (const auto& variant : variants->array) {
        Failure attempt;
        if (Match(variant.AsInt(-1), value, path, depth, &attempt)) return true;
        if (attempt.resource || attempt.internal) {
          *failure = std::move(attempt);
          return false;
        }
        if (attempt.path.size() >= best.path.size()) best = std::move(attempt);
      }
      *failure = std::move(best);
      return false;
    }
    if (kind == "array" || kind == "tuple") {
      if (!value.IsArray()) return Fail(path, "Expected an array", failure);
      const auto* tuple = node.GetArray("elements");
      if (kind == "tuple" && (!tuple || node.GetInt("required", -1) < 0))
        return Internal(path, failure);
      if (kind == "tuple" &&
          (value.array.size() < static_cast<size_t>(node.GetInt("required")) ||
           value.array.size() > tuple->array.size()))
        return Fail(path, "Invalid tuple length", failure);
      for (size_t i = 0; i < value.array.size(); ++i) {
        const auto child = kind == "array" ? node.GetInt("element", -1)
                                           : tuple->array[i].AsInt(-1);
        if (!Match(child, value.array[i], path + '[' + std::to_string(i) + ']',
                   depth + 1, failure))
          return false;
      }
      return true;
    }
    if (kind == "object") {
      if (!value.IsObject()) return Fail(path, "Expected an object", failure);
      const auto* fields = node.GetArray("fields");
      const auto* additional = node.Find("additional");
      if (!fields || !additional) return Internal(path, failure);
      std::unordered_set<std::string> present;
      for (const auto& [name, item] : value.object) {
        if (++work_ > 2 * 1024 * 1024)
          return Fail(path, "Item validation resource limit exceeded", failure,
                      true);
        if (!present.insert(name).second)
          return Fail(FieldPath(path, name), "Duplicate item field", failure);
        const auto field = std::find_if(
            fields->array.begin(), fields->array.end(),
            [&](const auto& field) { return field.GetString("name") == name; });
        if (field == fields->array.end() && additional->IsNull())
          return Fail(FieldPath(path, name), "Unknown item field", failure);
        const auto child = field == fields->array.end()
                               ? additional->AsInt(-1)
                               : field->GetInt("type", -1);
        if (field_bounds_ && field != fields->array.end()) {
          const auto* bounds = field->Find("bounds");
          if (bounds && !WithinBounds(item, *bounds))
            return Fail(FieldPath(path, name), "Field exceeds its size bounds",
                        failure);
        }
        if (!Match(child, item, FieldPath(path, name), depth + 1, failure))
          return false;
      }
      for (const auto& field : fields->array) {
        const auto name = field.GetString("name");
        if (!field.GetBool("optional") && !present.contains(name))
          return Fail(FieldPath(path, name), "Required item field is missing",
                      failure);
      }
      return true;
    }
    return Internal(path, failure);
  }

 private:
  static bool TextWithinBounds(std::string_view text, const Json& bounds) {
    // UTF-8 was validated by the request parser. Schema lengths count scalars.
    const auto count = std::count_if(text.begin(), text.end(),
        [](unsigned char byte) { return (byte & 0xc0) != 0x80; });
    return count >= bounds.GetInt("minLength") &&
           count <= bounds.GetInt("maxLength");
  }
  static bool WithinBounds(const Json& item, const Json& bounds) {
    if (item.IsNumber() && bounds.Has("maximum"))
      return item.number >= bounds.GetNumber("minimum") &&
             item.number <= bounds.GetNumber("maximum");
    if (item.IsString() && bounds.Has("maxLength"))
      return TextWithinBounds(item.str, bounds);
    if (item.IsArray()) {
      if (item.array.size() > static_cast<size_t>(bounds.GetInt("maxItems")))
        return false;
      const auto* elements = bounds.Find("items");
      return !elements || std::ranges::all_of(item.array, [&](const Json& v) {
        return !v.IsString() || TextWithinBounds(v.str, *elements);
      });
    }
    if (item.IsObject() && bounds.Has("maxProperties")) {
      if (item.object.size() > static_cast<size_t>(bounds.GetInt("maxProperties")))
        return false;
      const auto& names = *bounds.Find("propertyNames");
      const auto& values = *bounds.Find("additionalProperties");
      return std::ranges::all_of(item.object, [&](const auto& entry) {
        return TextWithinBounds(entry.first, names) &&
               (!entry.second.IsString() || TextWithinBounds(entry.second.str, values));
      });
    }
    return true;  // Nullability and field types are checked by the SDK graph.
  }
  static bool Fail(const std::string& path, std::string message,
                   Failure* failure, bool resource = false) {
    *failure = {path, std::move(message), resource, false};
    return false;
  }
  static bool Internal(const std::string& path, Failure* failure) {
    *failure = {path, "Compiled item contract is invalid", false, true};
    return false;
  }
  const std::vector<Json>& nodes_;
  bool field_bounds_;
  size_t work_ = 0;
};
Status Validate(const Graph& graph, const Json& item, const char* root,
                ChatPlanError* error, std::string_view path,
                bool field_bounds = false) {
  if (!error) return Status::Fail("Missing item validation error owner");
  *error = {};
  if (!graph.status) {
    *error = {"item_contract_unavailable", std::string(path), 503};
    return graph.status;
  }
  Validator validator(*graph.value.GetArray("nodes"), field_bounds);
  Failure failure;
  if (validator.Match(graph.value.Find("roots")->GetInt(root, -1), item,
                      std::string(path), 0, &failure))
    return {};
  *error = {failure.internal   ? "item_contract_unavailable"
            : failure.resource ? "item_validation_limit_exceeded"
                               : "invalid_request",
            std::move(failure.path),
            failure.internal || failure.resource ? 503 : 400};
  return Status::Fail(std::move(failure.message));
}
// Creation limits apply to uploaded bytes. Stored workspace copies and
// previous files retained by a metadata-only update are outside this request.
Status CheckInlineFile(const Json& file, const std::string& param,
                       size_t* total, ChatPlanError* error) {
  if (file.GetString("type") != "inline") return {};
  const auto* data = file.Find("data");
  size_t size = 0;
  constexpr size_t kPerFile = 5 * 1024 * 1024;
  constexpr size_t kPerRequest = 2 * kPerFile;
  if (!data || !data->IsString() || !io::Base64DecodedSize(data->str, &size)) {
    *error = {"invalid_request", param, 400};
    return Status::Fail("Expected canonical base64 file data");
  }
  if (size > kPerFile || size > kPerRequest - *total) {
    *error = {"invalid_request", param, 400};
    return Status::Fail(size > kPerFile ? "Inline file exceeds 5 MiB"
                                       : "Inline files exceed 10 MiB per request");
  }
  *total += size;
  return {};
}

}  // namespace

Status vault_detail::ValidateCredential(const Json& value, CredentialShape shape,
                                      ChatPlanError* error) {
  static const Graph graph(vault_credential_types::kData,
                           "q4t-openai-vault-credential-types-v1");
  const char* root = "";
  switch (shape) {
    case CredentialShape::kCreate: root = "create"; break;
    case CredentialShape::kAuthCreate: root = "auth_create"; break;
    case CredentialShape::kAuthRotate: root = "auth_rotate"; break;
    case CredentialShape::kResource: root = "resource"; break;
  }
  return Validate(graph, value, root, error, "");
}

Status ValidateEnvironmentTemplate(const Json& value, EnvironmentTemplateShape shape,
                                   ChatPlanError* error) {
  const char* root = "";
  switch (shape) {
    case EnvironmentTemplateShape::kStoredDefinition:
    case EnvironmentTemplateShape::kCreate: root = "template_create"; break;
    case EnvironmentTemplateShape::kUpdate: root = "template_update"; break;
    case EnvironmentTemplateShape::kResource: root = "template_resource"; break;
  }
  const bool incoming = shape == EnvironmentTemplateShape::kCreate ||
                        shape == EnvironmentTemplateShape::kUpdate;
  const auto valid = Validate(EnvironmentTypes(), value, root, error, "", incoming);
  const auto* files = value.GetArray("files");
  if (!valid || !incoming || !files) return valid;
  size_t total = 0;
  for (size_t i = 0; i < files->array.size(); ++i) {
    auto bytes = CheckInlineFile(files->array[i],
                                 "files[" + std::to_string(i) + "].data",
                                 &total, error);
    if (!bytes) return bytes;
  }
  return {};
}

Status ValidateEnvironment(const Json& value, EnvironmentShape shape,
                           ChatPlanError* error) {
  const char* root = "";
  switch (shape) {
    case EnvironmentShape::kCreate: root = "environment_create"; break;
    case EnvironmentShape::kResource: root = "environment_resource"; break;
    case EnvironmentShape::kQuery: root = "environment_query"; break;
    case EnvironmentShape::kFileCreate: root = "file_create"; break;
    case EnvironmentShape::kFileResource: root = "file_resource"; break;
    case EnvironmentShape::kFileQuery: root = "file_query"; break;
  }
  auto valid = Validate(EnvironmentTypes(), value, root, error, "");
  if (!valid || shape != EnvironmentShape::kFileCreate) return valid;
  size_t total = 0;
  return CheckInlineFile(value, "data", &total, error);
}

Status ValidateAgentSession(const Json& value, AgentSessionShape shape,
                            ChatPlanError* error) {
  static const Graph graph(agent_session_types::kData,
                           "q4t-openai-agent-session-types-v1");
  const char* root = "";
  switch (shape) {
    case AgentSessionShape::kCreate: root = "create"; break;
    case AgentSessionShape::kUpdate: root = "update"; break;
    case AgentSessionShape::kResource: root = "resource"; break;
    case AgentSessionShape::kDeleted: root = "deleted"; break;
    case AgentSessionShape::kQuery: root = "query"; break;
    case AgentSessionShape::kInputEvent: root = "input_event"; break;
    case AgentSessionShape::kEvent: root = "event"; break;
    case AgentSessionShape::kItem: root = "item"; break;
    case AgentSessionShape::kTurn: root = "turn"; break;
    case AgentSessionShape::kSubagent: root = "subagent"; break;
  }
  return Validate(graph, value, root, error, "");
}

Status ValidateAssistant(const Json& value, AssistantShape shape,
                         ChatPlanError* error) {
  static const Graph graph(assistant_types::kData,
                           "q4t-openai-assistant-types-v1");
  const char* root = "";
  switch (shape) {
    case AssistantShape::kCreate: root = "create"; break;
    case AssistantShape::kUpdate: root = "update"; break;
    case AssistantShape::kResource: root = "resource"; break;
    case AssistantShape::kQuery: root = "query"; break;
  }
  return Validate(graph, value, root, error, "", true);
}

Status ValidateAgentRegistration(const Json& value, AgentRegistrationShape shape,
                                 ChatPlanError* error) {
  static const Graph graph(agent_registration_types::kData,
                           "q4t-openai-agent-registration-types-v1");
  const char* root = "";
  switch (shape) {
    case AgentRegistrationShape::kCreate: root = "create"; break;
    case AgentRegistrationShape::kUpdate: root = "update"; break;
    case AgentRegistrationShape::kResource: root = "resource"; break;
    case AgentRegistrationShape::kDeleted: root = "deleted"; break;
  }
  return Validate(graph, value, root, error, "");
}

Status ValidateMultiAgentShape(const Json& value, MultiAgentShape shape,
                               ChatPlanError* error, std::string_view path) {
  static const Graph graph(multi_agent_types::kData,
                           "q4t-openai-multi-agent-types-v1");
  const char* root = "";
  switch (shape) {
    case MultiAgentShape::kConfiguration: root = "configuration"; break;
    case MultiAgentShape::kInputCall: root = "input_call"; break;
    case MultiAgentShape::kInputResult: root = "input_result"; break;
    case MultiAgentShape::kOutputCall: root = "output_call"; break;
    case MultiAgentShape::kOutputResult: root = "output_result"; break;
    case MultiAgentShape::kOutputMessage: root = "output_message"; break;
  }
  return Validate(graph, value, root, error, path);
}

Status ValidateResponsesItemShape(const Json& item, ResponsesItemShape shape,
                                  ChatPlanError* error, std::string_view path,
                                  bool multi_agent) {
  if (multi_agent && shape != ResponsesItemShape::kConversation) {
    const auto type = item.GetString("type");
    if (type == "message" && item.GetString("role") == "assistant" &&
        item.Has("agent"))
      return ValidateMultiAgentShape(item, MultiAgentShape::kOutputMessage,
                                     error, path);
    const bool input = shape == ResponsesItemShape::kInput;
    if (type == "multi_agent_call")
      return ValidateMultiAgentShape(item, input ? MultiAgentShape::kInputCall
                                                 : MultiAgentShape::kOutputCall,
                                     error, path);
    if (type == "multi_agent_call_output")
      return ValidateMultiAgentShape(item, input ? MultiAgentShape::kInputResult
                                                 : MultiAgentShape::kOutputResult,
                                     error, path);
  }
  const char* root = shape == ResponsesItemShape::kInput    ? "input_item"
                     : shape == ResponsesItemShape::kOutput ? "output_item"
                     : shape == ResponsesItemShape::kConversation
                         ? "conversation_item"
                         : "";
  return Validate(Types(), item, root, error, path);
}
Status ValidateDecisionShape(const Json& value, DecisionShape shape,
                             ChatPlanError* error) {
  static const Graph graph(decision_types::kData,
                           "q4t-openai-decision-types-v1");
  return Validate(graph, value, shape == DecisionShape::kRequest ? "request" : "decision",
                  error, shape == DecisionShape::kRequest ? "body" : "decision");
}
Status ValidateRealtimeShape(const Json& value, RealtimeShape shape,
                             ChatPlanError* error, std::string_view path) {
  const char* root = "";
  switch (shape) {
    case RealtimeShape::kItem: root = "conversation_item"; break;
    case RealtimeShape::kClientEvent: root = "client_event"; break;
    case RealtimeShape::kServerEvent: root = "server_event"; break;
    case RealtimeShape::kResponseCreate: root = "response_create"; break;
    case RealtimeShape::kSession: root = "session"; break;
    case RealtimeShape::kResponse: root = "response"; break;
    case RealtimeShape::kCallAccept: root = "call_accept"; break;
    case RealtimeShape::kCallRefer: root = "call_refer"; break;
    case RealtimeShape::kCallReject: root = "call_reject"; break;
    case RealtimeShape::kClientSecretCreate: root = "client_secret_create"; break;
    case RealtimeShape::kClientSecretCreated: root = "client_secret_created"; break;
  }
  return Validate(RealtimeTypes(), value, root, error, path);
}
Status ValidateLiveShape(const Json& value, LiveShape shape,
                         ChatPlanError* error, std::string_view path) {
  size_t storage = 0;
  if (!MeasureRealtimeStorage(value, 16ULL << 20, &storage)) {
    if (error) *error = {"item_validation_limit_exceeded", std::string(path), 503};
    return Status::Fail("Live event exceeds validation storage limit");
  }
  const char* root = "";
  switch (shape) {
    case LiveShape::kClientEvent: root = "client_event"; break;
    case LiveShape::kServerEvent: root = "server_event"; break;
    case LiveShape::kSessionStart: root = "session_start"; break;
    case LiveShape::kSessionUpdate: root = "session_update"; break;
    case LiveShape::kSession: root = "session"; break;
    case LiveShape::kMediaCreate: root = "media_create"; break;
    case LiveShape::kMediaCreated: root = "media_created"; break;
    case LiveShape::kSidebandClient: root = "sideband_client"; break;
    case LiveShape::kSidebandServer: root = "sideband_server"; break;
    case LiveShape::kForkClient: root = "fork_client"; break;
    case LiveShape::kForkServer: root = "fork_server"; break;
    case LiveShape::kSessionAccept: root = "session_accept"; break;
    case LiveShape::kSessionFork: root = "session_fork"; break;
    case LiveShape::kSessionForked: root = "session_forked"; break;
    case LiveShape::kSessionRefer: root = "session_refer"; break;
    case LiveShape::kSessionReject: root = "session_reject"; break;
  }
  return Validate(LiveTypes(), value, root, error, path);
}
}  // namespace q4t::server
