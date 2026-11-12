#include "q4t/server/conversation_state.h"
#include "q4t/server/tool_result_contracts.h"

#include <algorithm>
#include <unordered_set>

#include "object_protocol.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/responses_item_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kMaxConversationItems = 4096;
ObjectReply Invalid(std::string message, std::string param = {}) {
  auto error = Error(400, "invalid_request", std::move(message));
  error.value = Object({{"param", String(std::move(param))}});
  return error;
}
ObjectReply StateError() {
  return Error(503, "conversation_state_invalid",
               "Conversation has inconsistent stored state");
}
ObjectReply Result(Json value) {
  ObjectReply result;
  result.value = std::move(value);
  return result;
}
bool ValidScope(const StateAccess& access) {
  return !access.scope.empty() && access.scope.size() <= 256;
}
size_t Characters(std::string_view value) {
  return std::count_if(value.begin(), value.end(), [](unsigned char byte) {
    return (byte & 0xc0) != 0x80;
  });
}
ObjectReply Metadata(const Json& metadata) {
  if (metadata.IsNull()) return Result(Object());
  if (!metadata.IsObject() || metadata.object.size() > 16)
    return Invalid("metadata requires at most 16 entries", "metadata");
  std::unordered_set<std::string> seen;
  for (const auto& [key, value] : metadata.object) {
    if (Characters(key) > 64 || !value.IsString() ||
        Characters(value.str) > 512 || !seen.insert(key).second)
      return Invalid("metadata keys/values require at most 64/512 characters",
                     InputParamPath("metadata", key));
  }
  return Result(metadata);
}
ObjectReply ValidateItems(const Json& items) {
  if (!items.IsArray() || items.array.size() > 20)
    return Invalid("At most 20 items may be added at a time", "items");
  std::unordered_set<std::string> ids;
  for (size_t i = 0; i < items.array.size(); ++i) {
    const auto& item = items.array[i];
    const auto path = "items[" + std::to_string(i) + ']';
    ChatPlanError error;
    const auto type = item.GetString("type");
    const bool custom =
        type == "custom_tool_call" || type == "custom_tool_call_output";
    auto status = ValidateResponsesItemShape(
        custom ? ProjectStoredItem(item, {}, StoredItemView::kConversation)
               : item,
        ResponsesItemShape::kConversation, &error, path);
    if (!status) {
      auto result = Error(error.http_status, error.code, status.message());
      result.value = Object({{"param", String(error.param)}});
      return result;
    }
    const auto id = item.GetString("id");
    if (!ValidId(id) || !ids.insert(id).second)
      return Invalid("Invalid or repeated item identity", path + ".id");
  }
  return {};
}
ObjectRequest Write(ObjectKind kind, const std::string& id, int64_t revision,
                    Json data, const StateAccess& access) {
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.kind = kind;
  request.id = id;
  request.scope = access.scope;
  request.expected_revision = revision;
  request.ttl_seconds = 0;
  request.data = std::move(data);
  return request;
}
void Hide(Json* value, const std::string& key, bool nullable = true) {
  if (!value->IsObject()) return;
  if (nullable) {
    if (value->Has(key)) Set(value, key, Json());
  } else {
    std::erase_if(value->object,
                  [&](const auto& field) { return field.first == key; });
  }
}
Json Page(Json items, bool more) {
  const auto first =
      items.array.empty() ? "" : items.array.front().GetString("id");
  const auto last =
      items.array.empty() ? "" : items.array.back().GetString("id");
  return Object({{"object", String("list")},
                 {"data", std::move(items)},
                 {"first_id", String(first)},
                 {"last_id", String(last)},
                 {"has_more", Boolean(more)}});
}
}  // namespace

ObjectReply NormalizeConversationMetadata(const Json& metadata) {
  return Metadata(metadata);
}

Status ParseItemViewOptions(const Json& query, ItemViewOptions* options,
                            ChatPlanError* error, bool pagination) {
  *options = {};
  *error = {};
  auto status = pagination
                    ? InputFields(query, {"after", "limit", "order", "include"},
                                  &error->param, "query")
                    : InputFields(query, {"include"}, &error->param, "query");
  if (!status) return status;
  const auto fail = [&](std::string field, std::string message) {
    *error = {"invalid_request", std::move(field), 400};
    return Status::Fail(std::move(message));
  };
  if (const auto* limit = query.Find("limit")) {
    if (!IsInputInteger(*limit) || limit->number < 1 || limit->number > 100)
      return fail("limit", "limit requires an integer between 1 and 100");
    options->limit = limit->AsInt();
  }
  if (const auto* after = query.Find("after")) {
    if (!after->IsString() || !ValidId(after->str))
      return fail("after", "Invalid item cursor");
    options->after = after->str;
  }
  if (const auto* order = query.Find("order")) {
    if (!order->IsString() || (order->str != "asc" && order->str != "desc"))
      return fail("order", "order requires asc or desc");
    options->descending = order->str == "desc";
  }
  if (const auto* include = query.Find("include")) {
    constexpr std::string_view allowed[] = {
        "file_search_call.results",
        "web_search_call.results",
        "web_search_call.action.sources",
        "message.input_image.image_url",
        "computer_call_output.output.image_url",
        "code_interpreter_call.outputs",
        "reasoning.encrypted_content",
        "message.output_text.logprobs"};
    if (!include->IsArray() || include->array.size() > std::size(allowed))
      return fail("include", "Invalid include list");
    std::unordered_set<std::string> seen;
    for (const auto& item : include->array) {
      if (!item.IsString() ||
          std::find(std::begin(allowed), std::end(allowed), item.str) ==
              std::end(allowed) ||
          !seen.insert(item.str).second)
        return fail("include", "Invalid or duplicate include option");
      options->include.push_back(item.str);
    }
  }
  return {};
}

Json ProjectStoredItem(const Json& item, const ItemViewOptions& options,
                       StoredItemView view) {
  auto result = item;
  const auto included = [&](std::string_view key) {
    return std::find(options.include.begin(), options.include.end(), key) !=
           options.include.end();
  };
  const auto type = result.GetString("type");
  // The SDK's conversation union uses the base custom-call types. Its
  // Responses input_items list uses the Item variants with status instead.
  if (view == StoredItemView::kConversation &&
      (type == "custom_tool_call" || type == "custom_tool_call_output")) {
    Hide(&result, "status", false);
    Hide(&result, "created_by", false);
  }
  if (type == "file_search_call" && !included("file_search_call.results"))
    Hide(&result, "results");
  if (type == "web_search_call") {
    if (!included("web_search_call.results")) Hide(&result, "results");
    if (result.Has("action") && !included("web_search_call.action.sources"))
      Hide(&Field(result, "action"), "sources", false);
  }
  if (type == "code_interpreter_call" &&
      !included("code_interpreter_call.outputs"))
    Hide(&result, "outputs");
  if (type == "computer_call_output" && result.Has("output") &&
      !included("computer_call_output.output.image_url"))
    Hide(&Field(result, "output"), "image_url", false);
  if (type == "reasoning" && !included("reasoning.encrypted_content"))
    Hide(&result, "encrypted_content");
  if (type == "message" && result.GetArray("content")) {
    for (auto& part : Field(result, "content").array) {
      if (part.GetString("type") == "input_image" &&
          !included("message.input_image.image_url"))
        Hide(&part, "image_url");
      if (part.GetString("type") == "output_text" &&
          !included("message.output_text.logprobs"))
        Hide(&part, "logprobs", false);
    }
  }
  return result;
}

ObjectReply PageStoredItems(const Json& items, const ItemViewOptions& options,
                            StoredItemView view) {
  if (!items.IsArray()) return StateError();
  if (options.limit < 1 || options.limit > 100)
    return Invalid("limit requires an integer between 1 and 100", "limit");
  const auto size = items.array.size();
  size_t start = 0;
  if (!options.after.empty()) {
    const auto found = std::find_if(
        items.array.begin(), items.array.end(), [&](const auto& item) {
          return item.GetString("id") == options.after;
        });
    if (found == items.array.end())
      return Invalid("Cursor does not belong to this item list", "after");
    const auto index = static_cast<size_t>(found - items.array.begin());
    start = options.descending ? size - index : index + 1;
  }
  auto page = Array();
  const auto end = std::min(size, start + options.limit);
  for (size_t i = start; i < end; ++i)
    page.array.push_back(ProjectStoredItem(
        items.array[options.descending ? size - 1 - i : i], options, view));
  return Result(Page(std::move(page), end < size));
}

ObjectReply ConversationState::Read(const std::string& id,
                                    const StateAccess& access) {
  if (!ValidScope(access))
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope unavailable");
  if (!ValidId(id)) return Invalid("Invalid conversation identity");
  ObjectRequest request;
  request.kind = ObjectKind::kConversation;
  request.id = id;
  request.scope = access.scope;
  auto result = objects_.Execute(request, access.deadline, access.cancelled);
  if (!result.ok()) return result;
  const auto* data = result.value.Find("data");
  const auto* resource = data ? data->Find("resource") : nullptr;
  const auto* items = data ? data->GetArray("items") : nullptr;
  if (!resource || !items || resource->GetString("id") != id ||
      resource->GetString("object") != "conversation" ||
      result.value.GetInt("revision") < 1 ||
      items->array.size() > kMaxConversationItems)
    return StateError();
  return result;
}

ObjectReply ConversationState::Publish(const std::string& id, int64_t revision,
                                       Json data, const Json& new_items,
                                       const StateAccess& access) {
  std::vector<ObjectRequest> mutations;
  auto bindings =
      data.Find("input_snapshots") ? *data.Find("input_snapshots") : Json();
  const auto* all_items = data.GetArray("items");
  if (!all_items) return StateError();
  auto checked = MergeInputSnapshots(Json(), *all_items, &bindings);
  if (!checked.ok()) return checked;
  auto contracts =
      data.Find("tool_contracts") ? *data.Find("tool_contracts") : Json();
  checked = MergeToolResultContracts(Json(), *all_items, &contracts);
  if (!checked.ok()) return checked;
  for (const auto& item : new_items.array) {
    ObjectRequest get;
    get.kind = ObjectKind::kConversationItem;
    get.scope = access.scope;
    get.id = item.GetString("id");
    auto previous = objects_.Execute(get, access.deadline, access.cancelled);
    int64_t item_revision = 0;
    if (previous.ok()) {
      const auto* old_data = previous.value.Find("data");
      const auto* old_item = old_data ? old_data->Find("item") : nullptr;
      if (!old_item) return StateError();
      auto identical = CompareStoredItem(*old_item, item);
      if (!identical.ok()) return identical;
      if (const auto* bound = old_data->Find("input_snapshots")) {
        checked = MergeInputSnapshots(*bound, *all_items, &bindings);
        if (!checked.ok()) return checked;
      }
      if (const auto* bound = old_data->Find("tool_contracts")) {
        checked = MergeToolResultContracts(*bound, *all_items, &contracts);
        if (!checked.ok()) return checked;
      }
      item_revision = previous.value.GetInt("revision");
    } else if (previous.http_status != 404) {
      return previous;
    }
    Json own;
    checked = SelectInputSnapshots(bindings, Array({item}), &own);
    if (!checked.ok()) return checked;
    // Manually added calls have no declaring request. Existing stored calls
    // retain their original schema; a later tools list cannot replace it.
    checked = BindToolResultContracts(Object(), Array({item}), &contracts);
    if (!checked.ok()) return checked;
    Json own_contracts;
    checked =
        MergeToolResultContracts(contracts, Array({item}), &own_contracts);
    if (!checked.ok()) return checked;
    mutations.push_back(Write(ObjectKind::kConversationItem, get.id,
                              item_revision,
                              Object({{"item", item},
                                      {"input_snapshots", own},
                                      {"tool_contracts", own_contracts}}),
                              access));
  }
  Set(&data, "input_snapshots", std::move(bindings));
  Set(&data, "tool_contracts", std::move(contracts));
  mutations.insert(mutations.begin(), Write(ObjectKind::kConversation, id,
                                            revision, std::move(data), access));
  return objects_.ExecuteBatch(mutations, access.deadline, access.cancelled);
}

ObjectReply ConversationState::Create(const Json& metadata, const Json& items,
                                      const StateAccess& access) {
  if (!ValidScope(access))
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope unavailable");
  auto normalized = Metadata(metadata);
  if (!normalized.ok()) return normalized;
  auto valid = ValidateItems(items);
  if (!valid.ok()) return valid;
  const auto id = NewObjectId(ObjectKind::kConversation);
  if (id.empty())
    return Error(503, "object_identity_unavailable",
                 "Conversation identity unavailable");
  const auto created_at =
      std::chrono::duration_cast<std::chrono::seconds>(
          std::chrono::system_clock::now().time_since_epoch())
          .count();
  auto resource = Object({{"id", String(id)},
                          {"object", String("conversation")},
                          {"created_at", Number(created_at)},
                          {"metadata", normalized.value}});
  auto written = Publish(
      id, 0, Object({{"resource", resource}, {"items", items}}), items, access);
  if (!written.ok()) {
    // A lost receipt must be reconciled by this exact generated identity.
    if (written.commit_unknown) written.value = Object({{"id", String(id)}});
    return written;
  }
  return Result(std::move(resource));
}

ObjectReply ConversationState::Retrieve(const std::string& id,
                                        const StateAccess& access) {
  auto existing = Read(id, access);
  if (!existing.ok()) return existing;
  return Result(*existing.value.Find("data")->Find("resource"));
}

ObjectReply ConversationState::Update(const std::string& id,
                                      const Json& metadata,
                                      const StateAccess& access) {
  auto normalized = Metadata(metadata);
  if (!normalized.ok()) return normalized;
  auto existing = Read(id, access);
  if (!existing.ok()) return existing;
  auto data = *existing.value.Find("data");
  Set(&Field(data, "resource"), "metadata", normalized.value);
  auto resource = *data.Find("resource");
  auto written = objects_.Execute(
      Write(ObjectKind::kConversation, id, existing.value.GetInt("revision"),
            std::move(data), access),
      access.deadline, access.cancelled);
  return written.ok() ? Result(std::move(resource)) : written;
}

ObjectReply ConversationState::Erase(const std::string& id,
                                     const StateAccess& access) {
  auto existing = Read(id, access);
  if (!existing.ok()) return existing;
  ObjectRequest request;
  request.kind = ObjectKind::kConversation;
  request.operation = ObjectOperation::kErase;
  request.scope = access.scope;
  request.id = id;
  request.expected_revision = existing.value.GetInt("revision");
  auto written = objects_.Execute(request, access.deadline, access.cancelled);
  return written.ok()
             ? Result(Object({{"id", String(id)},
                              {"object", String("conversation.deleted")},
                              {"deleted", Boolean(true)}}))
             : written;
}

ObjectReply ConversationState::Append(const std::string& id, const Json& items,
                                      const ItemViewOptions& options,
                                      const StateAccess& access) {
  auto valid = ValidateItems(items);
  if (!valid.ok()) return valid;
  auto existing = Read(id, access);
  if (!existing.ok()) return existing;
  auto data = *existing.value.Find("data");
  auto& current = Field(data, "items").array;
  if (items.array.size() + current.size() > kMaxConversationItems)
    return Error(413, "conversation_item_limit",
                 "Conversation item limit exceeded");
  std::unordered_set<std::string> ids;
  for (const auto& item : current) ids.insert(item.GetString("id"));
  for (const auto& item : items.array) {
    if (!ids.insert(item.GetString("id")).second)
      return Error(409, "item_already_in_conversation",
                   "Item already belongs to this conversation");
    current.push_back(item);
  }
  auto written = Publish(id, existing.value.GetInt("revision"), std::move(data),
                         items, access);
  if (!written.ok()) return written;
  auto result = Array();
  for (const auto& item : items.array)
    result.array.push_back(
        ProjectStoredItem(item, options, StoredItemView::kConversation));
  return Result(Page(std::move(result), false));
}

ObjectReply ConversationState::ListItems(const std::string& id,
                                         const ItemViewOptions& options,
                                         const StateAccess& access) {
  auto existing = Read(id, access);
  if (!existing.ok()) return existing;
  return PageStoredItems(*existing.value.Find("data")->Find("items"), options,
                         StoredItemView::kConversation);
}

ObjectReply ConversationState::RetrieveItem(const std::string& id,
                                            const std::string& item_id,
                                            const ItemViewOptions& options,
                                            const StateAccess& access) {
  auto existing = Read(id, access);
  if (!existing.ok()) return existing;
  const auto* items = existing.value.Find("data")->GetArray("items");
  for (const auto& item : items->array)
    if (item.GetString("id") == item_id)
      return Result(
          ProjectStoredItem(item, options, StoredItemView::kConversation));
  return Error(404, "object_not_found",
               "Item does not belong to this conversation");
}

ObjectReply ConversationState::EraseItem(const std::string& id,
                                         const std::string& item_id,
                                         const StateAccess& access) {
  auto existing = Read(id, access);
  if (!existing.ok()) return existing;
  auto data = *existing.value.Find("data");
  auto& items = Field(data, "items").array;
  const auto removed = std::erase_if(
      items, [&](const auto& item) { return item.GetString("id") == item_id; });
  if (!removed)
    return Error(404, "object_not_found",
                 "Item does not belong to this conversation");
  if (const auto* bindings = data.Find("input_snapshots")) {
    Json retained;
    auto checked =
        SelectInputSnapshots(*bindings, *data.Find("items"), &retained);
    if (!checked.ok()) return checked;
    Set(&data, "input_snapshots", std::move(retained));
  }
  if (const auto* contracts = data.Find("tool_contracts")) {
    Json retained;
    auto checked =
        MergeToolResultContracts(*contracts, Field(data, "items"), &retained);
    if (!checked.ok()) return checked;
    Set(&data, "tool_contracts", std::move(retained));
  }
  auto resource = *data.Find("resource");
  auto written = objects_.Execute(
      Write(ObjectKind::kConversation, id, existing.value.GetInt("revision"),
            std::move(data), access),
      access.deadline, access.cancelled);
  return written.ok() ? Result(std::move(resource)) : written;
}
}  // namespace q4t::server
