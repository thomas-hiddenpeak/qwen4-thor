#include "eval_run_state.h"

#include <algorithm>

#include "q4t/server/binary_store.h"
#include "q4t/server/chat_storage.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_json.h"

namespace q4t::server::eval_detail {
namespace {
using namespace object_detail;

bool MatchMetadata(const Json* filter, const Json* metadata) {
  if (!filter || filter->IsNull()) return true;
  for (const auto& [key, value] : filter->object) {
    const auto* found = metadata ? metadata->Find(key) : nullptr;
    if (!found || !found->IsString() || value.str != found->str) return false;
  }
  return true;
}

bool Matches(const Json& query, const Json& config, const Json& response,
             int64_t created) {
  if (created < query.GetInt("created_after", INT64_MIN) ||
      created > query.GetInt("created_before", INT64_MAX))
    return false;
  const auto* metadata = response.Find("metadata");
  if (!MatchMetadata(config.Find("metadata"), metadata) ||
      !MatchMetadata(query.Find("metadata"), metadata))
    return false;
  for (const char* key : {"model", "temperature", "top_p"}) {
    const auto* needle = query.Find(key);
    if (!needle || needle->IsNull()) continue;
    const auto* actual = response.Find(key);
    if (!actual || (needle->IsNumber() ? !actual->IsNumber() ||
                                             needle->number != actual->number
                                       : needle->str != actual->str))
      return false;
  }
  if (const auto* needle = query.Find("reasoning_effort");
      needle && needle->IsString()) {
    const auto* reasoning = response.Find("reasoning");
    if (!reasoning || reasoning->GetString("effort") != needle->str)
      return false;
  }
  const auto search = query.GetString("instructions_search");
  if (!search.empty() &&
      response.GetString("instructions").find(search) == std::string::npos)
    return false;
  if (const auto* users = query.GetArray("users");
      users && !users->array.empty()) {
    const auto user = response.GetString("user");
    if (std::none_of(users->array.begin(), users->array.end(),
                     [&](const Json& v) { return v.str == user; }))
      return false;
  }
  if (const auto* tools = query.GetArray("tools");
      tools && !tools->array.empty()) {
    const auto* available = response.GetArray("tools");
    if (!available ||
        std::none_of(
            available->array.begin(), available->array.end(),
            [&](const Json& t) {
              const auto name = t.GetString("name", t.GetString("type"));
              return std::any_of(tools->array.begin(), tools->array.end(),
                                 [&](const Json& v) { return name == v.str; });
            }))
      return false;
  }
  return true;
}

ObjectReply DatasetRow(const Json& record, const Json& config,
                       const Json& query, bool chat, Json* row) {
  const auto* data = record.Find("data");
  const auto* response = data ? data->Find("response") : nullptr;
  if (!response || response->GetString("id") != record.GetString("id"))
    return InvalidRecord();
  if (!chat) {
    // Internal model work and temporary background retention are not user logs.
    if (data->Has("eval_run_id") || data->Has("agent_session_id") ||
        data->Has("assistant_thread_id") || data->Has("chatkit_thread_id") ||
        data->Has("batch_parent") || !response->GetBool("store"))
      return {};
    const auto status = response->GetString("status");
    if (status != "completed" && status != "incomplete") return {};
  }
  const auto created = response->GetInt(chat ? "created" : "created_at");
  if (!Matches(query, config, *response, created)) return {};
  const auto* messages = data->GetArray(chat ? "messages" : "history");
  const auto* output = response->GetArray(chat ? "choices" : "output");
  if (!output || !messages) return InvalidRecord();
  auto trajectory = Array(), outputs = Array();
  std::string text;
  if (chat) {
    for (auto message : messages->array) {
      const auto* parts = message.GetArray("content_parts");
      if (parts) Set(&message, "content", *parts);
      std::erase_if(message.object, [](const auto& f) {
        return f.first == "id" || f.first == "content_parts" ||
               (f.first == "name" && f.second.IsNull());
      });
      trajectory.array.push_back(std::move(message));
    }
    for (const auto& choice : output->array) {
      const auto* msg = choice.Find("message");
      if (!msg) return InvalidRecord();
      outputs.array.push_back(*msg);
    }
    if (!outputs.array.empty())
      text = outputs.array.front().GetString("content");
  } else {
    for (const auto& message : messages->array)
      trajectory.array.push_back(ProjectStoredItem(message, {}));
    for (const auto& result : output->array) {
      auto item = ProjectStoredItem(result, {});
      if (item.GetString("type") == "message")
        if (const auto* content = item.GetArray("content"))
          for (const auto& part : content->array)
            if (part.GetString("type") == "output_text")
              text += part.GetString("text");
      outputs.array.push_back(std::move(item));
    }
  }
  auto item = Object({{"id", *response->Find("id")},
                      {"model", String(response->GetString("model"))},
                      {"created_at", Number(created)},
                      {"input_trajectory", std::move(trajectory)},
                      {"output_trajectory", std::move(outputs)},
                      {"output_text", String(std::move(text))}});
  if (const auto* metadata = response->Find("metadata"))
    Set(&item, "metadata", *metadata);
  *row = Object({{"item", std::move(item)}});
  return {};
}
}  // namespace

ObjectReply PublishStoredRun(ObjectStore& objects, const StateAccess& access,
                             const Json& config, const Json& query,
                             ObjectRequest run, const ObjectRequest& parent) {
  const bool chat = query.GetString("type") == "stored_completions";
  const auto limit = query.GetInt("limit", INT64_MAX);
  if (limit < 0)
    return BadInput("data_source.source.limit", "Expected a nonnegative limit");
  if (const auto* filter = query.Find("metadata");
      filter && !filter->IsNull()) {
    std::string param;
    auto status = ValidateChatMetadata(*filter, &param);
    if (!status)
      return BadInput("data_source.source." + param, status.message());
  }
  BinaryAccess auth{access.scope, access.deadline, access.cancelled};
  BinaryStore binary(objects);
  BinaryStage stage;
  auto began = binary.Begin(auth, kBinaryMaxBytes, 900, &stage);
  if (!began.ok()) return began;
  ScopeExit cleanup([&] {
    if (!stage.closed && !stage.commit_unknown &&
        !binary.Abort(auth, &stage).ok())
      objects.RecordAuxiliaryCleanupFailure();
  });
  ObjectRequest page;
  page.operation = ObjectOperation::kList;
  page.kind = chat ? ObjectKind::kChat : ObjectKind::kResponse;
  page.scope = access.scope;
  page.descending = true;
  page.limit = 8;
  std::string buffer;
  int64_t rows = 0;
  while (rows < limit) {
    auto records = objects.Execute(page, access.deadline, access.cancelled);
    if (!records.ok()) return records;
    const auto* entries = records.value.GetArray("data");
    if (!entries) return InvalidRecord();
    for (const auto& record : entries->array) {
      Json row;
      auto selected = DatasetRow(record, config, query, chat, &row);
      if (!selected.ok()) return selected;
      if (row.IsNull()) continue;
      auto line = OutputJson(row);
      if (line.size() > kChatBodyBytes)
        return Error(413, "eval_row_too_large",
                     "Stored input exceeds row capacity");
      line.push_back('\n');
      for (std::string_view rest(line); !rest.empty();) {
        const auto take =
            std::min(rest.size(), kBinaryChunkBytes - buffer.size());
        buffer.append(rest.substr(0, take));
        rest.remove_prefix(take);
        if (buffer.size() == kBinaryChunkBytes) {
          auto appended = binary.Append(auth, &stage, buffer);
          if (!appended.ok()) return appended;
          buffer.clear();
        }
      }
      if (++rows == limit) break;
    }
    if (!records.value.GetBool("has_more")) break;
    const auto cursor = records.value.GetString("last_id");
    if (cursor.empty() || cursor == page.after) return InvalidRecord();
    page.after = cursor;
  }
  if (!buffer.empty()) {
    auto appended = binary.Append(auth, &stage, buffer);
    if (!appended.ok()) return appended;
  }
  return binary.Publish(auth, &stage, std::move(run), std::span(&parent, 1));
}
}  // namespace q4t::server::eval_detail
