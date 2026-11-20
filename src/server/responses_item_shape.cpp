#include "q4t/server/responses_item_shape.h"

#include <algorithm>
#include <unordered_set>

#include "q4t/server/input_contract.h"
#include "q4t/server/live_shape.h"
#include "q4t/server/live_types_data.h"
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
  explicit Validator(const Json& nodes) : nodes_(nodes.array) {}
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
  size_t work_ = 0;
};
Status Validate(const Graph& graph, const Json& item, const char* root,
                ChatPlanError* error, std::string_view path) {
  if (!error) return Status::Fail("Missing item validation error owner");
  *error = {};
  if (!graph.status) {
    *error = {"item_contract_unavailable", std::string(path), 503};
    return graph.status;
  }
  Validator validator(*graph.value.GetArray("nodes"));
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
}  // namespace

Status ValidateResponsesItemShape(const Json& item, ResponsesItemShape shape,
                                  ChatPlanError* error, std::string_view path) {
  const char* root = shape == ResponsesItemShape::kInput    ? "input_item"
                     : shape == ResponsesItemShape::kOutput ? "output_item"
                     : shape == ResponsesItemShape::kConversation
                         ? "conversation_item"
                         : "";
  return Validate(Types(), item, root, error, path);
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
