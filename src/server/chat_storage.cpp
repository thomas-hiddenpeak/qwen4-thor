#include "q4t/server/chat_storage.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/chat_file_inputs.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr std::string_view kRoot = "/v1/chat/completions";

ObjectReply Invalid(std::string message, std::string param) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}
ObjectReply Result(Json value) {
  ObjectReply result;
  result.value = std::move(value);
  return result;
}
ObjectReply StateError() {
  return Error(503, "chat_state_invalid", "Stored completion is inconsistent");
}
ObjectRequest Request(ObjectOperation operation, const std::string& id,
                      const StateAccess& access) {
  ObjectRequest request;
  request.operation = operation;
  request.kind = ObjectKind::kChat;
  request.id = id;
  request.scope = access.scope;
  return request;
}
Json Page(Json values, bool more) {
  auto first = values.array.empty()
                   ? Json()
                   : String(values.array.front().GetString("id"));
  auto last = values.array.empty()
                  ? Json()
                  : String(values.array.back().GetString("id"));
  return Object({{"object", String("list")},
                 {"data", std::move(values)},
                 {"first_id", std::move(first)},
                 {"last_id", std::move(last)},
                 {"has_more", Boolean(more)}});
}
bool ValidOptions(const ChatListOptions& options, bool messages) {
  std::string param;
  return options.limit >= 1 && options.limit <= 100 &&
         (options.after.empty() || ValidId(options.after)) &&
         ValidateChatMetadata(options.metadata, &param).ok() &&
         (!messages || (options.model.empty() && options.metadata.IsNull()));
}
const Json* Completion(const Json& record) {
  const auto* data = record.Find("data");
  const auto* response = data ? data->Find("response") : nullptr;
  const auto* messages = data ? data->Find("messages") : nullptr;
  return data && data->GetInt("version") == 1 && response &&
                 response->GetString("object") == "chat.completion" &&
                 response->GetString("id") == record.GetString("id") &&
                 response->GetInt("created") == record.GetInt("created_at") &&
                 response->GetArray("choices") && messages &&
                 messages->IsArray()
             ? response
             : nullptr;
}
bool Matches(const Json& response, const ChatListOptions& options) {
  if (!options.model.empty() && response.GetString("model") != options.model)
    return false;
  const auto* metadata = response.Find("metadata");
  for (const auto& [key, expected] : options.metadata.object) {
    const auto* actual = metadata ? metadata->Find(key) : nullptr;
    if (!actual || !actual->IsString() || actual->str != expected.str)
      return false;
  }
  return true;
}
ObjectReply ParseOptions(const Json& query, bool messages,
                         ChatListOptions* options) {
  *options = {};
  Json metadata = Object();
  bool null_metadata = false;
  for (const auto& [key, value] : query.object) {
    if (key == "limit") {
      if (!IsInputInteger(value) || value.number < 1 || value.number > 100)
        return Invalid("limit must be in [1,100]", "limit");
      options->limit = value.AsInt();
    } else if (key == "after") {
      if (!value.IsString() || !ValidId(value.str))
        return Invalid("Invalid completion/message cursor", "after");
      options->after = value.str;
    } else if (key == "order") {
      if (!value.IsString() || (value.str != "asc" && value.str != "desc"))
        return Invalid("order must be asc or desc", "order");
      options->descending = value.str == "desc";
    } else if (!messages && key == "model") {
      if (!value.IsString() || value.str.empty())
        return Invalid("model requires a nonempty string", "model");
      options->model = value.str;
    } else if (!messages && key == "metadata") {
      // The official SDK serializes nullable query metadata as metadata=.
      if (!value.IsString() || !value.str.empty())
        return Invalid("metadata requires nested keys or an empty value",
                       "metadata");
      null_metadata = true;
    } else if (!messages && key.starts_with("metadata[") &&
               key.ends_with(']')) {
      Set(&metadata, key.substr(9, key.size() - 10), value);
    } else {
      return Invalid("Unsupported query field", key);
    }
  }
  if (null_metadata && !metadata.object.empty())
    return Invalid("Mixed null and object metadata encodings", "metadata");
  if (!metadata.object.empty()) options->metadata = std::move(metadata);
  std::string param;
  auto status = ValidateChatMetadata(options->metadata, &param);
  return status ? ObjectReply() : Invalid(status.message(), param);
}
}  // namespace

ObjectReply StoredChats::Save(const Json& request, const Json& response,
                              const StateAccess& access,
                              const Json& input_snapshots) {
  ChatStorageOptions options;
  std::string param;
  auto status = ParseChatStorageOptions(request, &options, &param);
  if (!status) return Invalid(status.message(), param);
  if (!options.store)
    return Invalid("Persistent completion requires store=true", "store");
  const auto* messages = request.Find("messages");
  Json prompt_messages;
  if (!messages && request.Find("prompt") &&
      request.Find("prompt")->IsString()) {
    prompt_messages = Array({Object(
        {{"role", String("user")}, {"content", *request.Find("prompt")}})});
    messages = &prompt_messages;
  }
  const auto id = response.GetString("id");
  const auto* created = response.Find("created");
  const auto* choices = response.GetArray("choices");
  if (!ValidId(id) || response.GetString("object") != "chat.completion" ||
      !created || !IsInputInteger(*created) || created->number <= 0 ||
      created->number > std::time(nullptr) || !choices ||
      choices->array.empty() || !messages || !messages->IsArray() ||
      messages->array.size() > 4096)
    return Invalid("Invalid generated completion or input messages", "store");
  for (const auto& choice : choices->array) {
    const auto finish = choice.GetString("finish_reason");
    const auto* message = choice.Find("message");
    if (!message || !message->IsObject() ||
        (finish != "stop" && finish != "length" && finish != "tool_calls" &&
         finish != "function_call" && finish != "content_filter"))
      return Invalid("Only complete generated choices can be stored", "store");
  }
  Json stored_messages = Array(), original_items = Array();
  for (size_t index = 0; index < messages->array.size(); ++index) {
    auto message = messages->array[index];
    if (!message.IsObject())
      return Invalid("Invalid input message", "messages");
    Set(&message, "id", String(StoredChatMessageId(id, index)));
    original_items.array.push_back(message);
    if (!message.Has("name")) Set(&message, "name", Json());
    const auto* content = message.Find("content");
    if (content && content->IsArray()) {
      auto parts = *content;
      std::string text;
      bool has_text = false;
      for (const auto& part : parts.array) {
        if (part.GetString("type") == "text") {
          text += part.GetString("text");
          has_text = true;
        }
      }
      Set(&message, "content", has_text ? String(std::move(text)) : Json());
      Set(&message, "content_parts", std::move(parts));
    } else {
      if (!content) Set(&message, "content", Json());
      Set(&message, "content_parts", Json());
    }
    stored_messages.array.push_back(std::move(message));
  }
  auto resource = response;
  Set(&resource, "metadata", options.metadata);
  auto write = Request(ObjectOperation::kPut, id, access);
  write.created_at = created->AsInt();
  write.data = Object({{"version", Number(1)},
                       {"response", resource},
                       {"messages", std::move(stored_messages)}});
  if (!input_snapshots.IsNull()) {
    Json verified;
    auto checked =
        MergeInputSnapshots(input_snapshots, original_items, &verified);
    if (!checked.ok()) return checked;
    Set(&write.data, "input_snapshots", std::move(verified));
  }
  auto result = objects_.Execute(write, access.deadline, access.cancelled);
  if (!result.ok()) return result;
  result.value = std::move(resource);
  return result;
}

ObjectReply StoredChats::Read(const std::string& id,
                              const StateAccess& access) {
  auto result = objects_.Execute(Request(ObjectOperation::kGet, id, access),
                                 access.deadline, access.cancelled);
  if (!result.ok()) return result;
  return Completion(result.value) ? result : StateError();
}
ObjectReply StoredChats::Retrieve(const std::string& id,
                                  const StateAccess& access) {
  auto result = Read(id, access);
  if (result.ok()) {
    auto response = *Completion(result.value);
    result.value = std::move(response);
  }
  return result;
}
ObjectReply StoredChats::Update(const std::string& id, const Json& metadata,
                                const StateAccess& access) {
  std::string param;
  auto status = ValidateChatMetadata(metadata, &param);
  if (!status) return Invalid(status.message(), param);
  auto result = Read(id, access);
  if (!result.ok()) return result;
  auto data = *result.value.Find("data");
  Set(&Field(data, "response"), "metadata", metadata);
  auto write = Request(ObjectOperation::kPut, id, access);
  write.expected_revision = result.value.GetInt("revision");
  // Updating metadata must not extend the original retention deadline.
  const auto expires = result.value.GetInt("expires_at");
  if (expires && expires <= std::time(nullptr))
    return Error(404, "object_not_found", "Object not found");
  write.expires_at = expires;
  write.data = std::move(data);
  result = objects_.Execute(write, access.deadline, access.cancelled);
  if (result.ok()) {
    const auto* response = Completion(result.value);
    if (!response) return StateError();
    auto value = *response;
    result.value = std::move(value);
  }
  return result;
}
ObjectReply StoredChats::Erase(const std::string& id,
                               const StateAccess& access) {
  auto result = objects_.Execute(Request(ObjectOperation::kErase, id, access),
                                 access.deadline, access.cancelled);
  if (result.ok())
    result.value = Object({{"id", String(id)},
                           {"deleted", Boolean(true)},
                           {"object", String("chat.completion.deleted")}});
  return result;
}
ObjectReply StoredChats::List(const ChatListOptions& options,
                              const StateAccess& access) {
  if (!ValidOptions(options, false))
    return Invalid("Invalid list options", "query");
  auto request = Request(ObjectOperation::kList, "", access);
  request.limit = 100;
  request.after = options.after;
  request.descending = options.descending;
  Json items = Array();
  size_t bytes = 0;
  for (;;) {
    auto result = objects_.Execute(request, access.deadline, access.cancelled);
    if (!result.ok()) return result;
    const auto* rows = result.value.GetArray("data");
    if (!rows) return StateError();
    for (const auto& row : rows->array) {
      const auto* response = Completion(row);
      if (!response) return StateError();
      if (!Matches(*response, options)) continue;
      const auto size = OutputJson(*response).size();
      if (items.array.size() == static_cast<size_t>(options.limit) ||
          (!items.array.empty() && bytes + size > kMaxFrame / 2))
        return Result(Page(std::move(items), true));
      items.array.push_back(*response);
      bytes += size;
    }
    if (!result.value.GetBool("has_more"))
      return Result(Page(std::move(items), false));
    const auto cursor = result.value.GetString("last_id");
    if (cursor.empty() || cursor == request.after) return StateError();
    request.after = cursor;
  }
}
ObjectReply StoredChats::Messages(const std::string& id,
                                  const ChatListOptions& options,
                                  const StateAccess& access) {
  if (!ValidOptions(options, true))
    return Invalid("Invalid list options", "query");
  auto result = Read(id, access);
  if (!result.ok()) return result;
  auto messages = *result.value.Find("data")->GetArray("messages");
  if (options.descending)
    std::reverse(messages.array.begin(), messages.array.end());
  size_t index = 0;
  if (!options.after.empty()) {
    while (index < messages.array.size() &&
           messages.array[index].GetString("id") != options.after)
      ++index;
    if (index == messages.array.size())
      return Invalid("Message cursor not in this completion", "after");
    ++index;
  }
  Json page = Array();
  for (; index < messages.array.size() &&
         page.array.size() < static_cast<size_t>(options.limit);
       ++index)
    page.array.push_back(std::move(messages.array[index]));
  return Result(Page(std::move(page), index < messages.array.size()));
}

ObjectReply FinalizeStoredChat(ObjectStore* objects, const Json& request,
                               const std::string& response,
                               const StateAccess& access,
                               const Json& input_snapshots) {
  ChatStorageOptions options;
  std::string param;
  auto status = ParseChatStorageOptions(request, &options, &param);
  if (!status) return Invalid(status.message(), param);
  Json value;
  status = Parse(response, &value);
  if (!status) return StateError();
  if (options.store) {
    if (!objects)
      return Error(503, "object_scope_unavailable",
                   "Application state unavailable");
    return StoredChats(*objects).Save(request, value, access, input_snapshots);
  }
  if (options.has_metadata) Set(&value, "metadata", options.metadata);
  return Result(std::move(value));
}

ObjectReply HandleStoredChatRequest(ObjectStore& objects,
                                    std::string_view method,
                                    std::string_view target,
                                    const std::string& body,
                                    const StateAccess& access) {
  const auto separator = target.find('?');
  const auto path = target.substr(0, separator);
  if (!IsStoredChatTarget(path))
    return Error(404, "route_not_found", "Stored chat route not found");
  const bool collection = path == kRoot;
  bool messages = false;
  std::string id;
  if (!collection) {
    auto suffix = path.substr(kRoot.size() + 1);
    const auto slash = suffix.find('/');
    id = suffix.substr(0, slash);
    if (!ValidId(id)) return Invalid("Invalid completion ID", "completion_id");
    if (slash != suffix.npos) {
      if (suffix.substr(slash) != "/messages")
        return Error(404, "route_not_found", "Stored chat route not found");
      messages = true;
    }
  }
  if ((collection || messages)
          ? method != "GET"
          : method != "GET" && method != "POST" && method != "DELETE") {
    auto result = Error(405, "method_not_allowed", "Method not allowed");
    result.value =
        Object({{"param", String("method")},
                {"allow", String(collection ? "GET, POST"
                                 : messages ? "GET"
                                            : "GET, POST, DELETE")}});
    return result;
  }
  if (method != "POST" && !body.empty())
    return Invalid("Body is not allowed", "body");
  auto query = ParseApplicationQuery(
      separator == target.npos ? "" : target.substr(separator + 1));
  if (!query.ok()) return query;
  ChatListOptions options;
  if (collection || messages) {
    auto result = ParseOptions(query.value, messages, &options);
    if (!result.ok()) return result;
  } else if (!query.value.object.empty()) {
    return Invalid("Query is not allowed", "query");
  }
  StoredChats chats(objects);
  if (collection) return chats.List(options, access);
  if (messages) return chats.Messages(id, options, access);
  if (method == "GET") return chats.Retrieve(id, access);
  if (method == "DELETE") return chats.Erase(id, access);
  Json request;
  auto status = io::ParseJson(body, &request, true, kSchemaJsonLimits);
  if (!status) return Invalid(status.message(), "body");
  std::string param;
  status = InputFields(request, {"metadata"}, &param);
  if (!status) return Invalid(status.message(), param);
  const auto* metadata = request.Find("metadata");
  if (!metadata) return Invalid("metadata is required", "metadata");
  return chats.Update(id, *metadata, access);
}
}  // namespace q4t::server
