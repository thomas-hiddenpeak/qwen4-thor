#include "assistant_threads_internal.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/files.h"
#include "q4t/server/http_response.h"

namespace q4t::server::assistant_detail {
namespace {
using namespace object_detail;
Json TextBlock(const Json& text) {
  return Object(
      {{"type", String("text")},
       {"text", Object({{"value", text}, {"annotations", Array()}})}});
}
}  // namespace

ObjectReply CreateMessage(ObjectStore& objects, const BinaryAccess& access,
                          const std::string& thread, const Json& input,
                          Attachments* attachments, Json* message) {
  ChatPlanError error;
  auto valid = ValidateAssistant(input, AssistantShape::kMessageCreate, &error);
  if (!valid) return Invalid(valid.message(), error.param);
  auto content = Array();
  const auto& body = *input.Find("content");
  if (body.IsString()) {
    content.array.push_back(TextBlock(body));
  } else {
    for (const auto& part : body.array) {
      if (part.GetString("type") == "text") {
        content.array.push_back(TextBlock(*part.Find("text")));
      } else {
        if (const auto* image = part.Find("image_file")) {
          auto file =
              Files(objects).Retrieve(access, image->GetString("file_id"));
          if (!file.ok()) return file;
        }
        content.array.push_back(part);
      }
    }
  }
  if (const auto* list = input.GetArray("attachments")) {
    for (const auto& item : list->array) {
      const auto id = item.GetString("file_id");
      const auto* tools = item.GetArray("tools");
      if (id.empty() || !tools)
        return Invalid("An attachment requires file_id and tools",
                       "attachments");
      auto file = Files(objects).Retrieve(access, id);
      if (!file.ok()) return file;
      for (const auto& tool : tools->array) {
        auto& ids = tool.GetString("type") == "file_search"
                        ? attachments->search
                        : attachments->code;
        if (std::ranges::find(ids, id) == ids.end()) ids.push_back(id);
      }
    }
  }
  const auto now = Number(std::time(nullptr));
  *message = Object({{"id", String(detail::NewRequestId("msg_"))},
                     {"object", String("thread.message")},
                     {"created_at", now},
                     {"completed_at", now},
                     {"thread_id", String(thread)},
                     {"role", *input.Find("role")},
                     {"content", std::move(content)},
                     {"assistant_id", {}},
                     {"run_id", {}},
                     {"status", String("completed")},
                     {"incomplete_at", {}},
                     {"incomplete_details", {}},
                     {"metadata", Object()},
                     {"attachments", Array()}});
  for (const auto* field : {"metadata", "attachments"})
    if (const auto* value = input.Find(field)) Set(message, field, *value);
  valid = ValidateAssistant(*message, AssistantShape::kMessageResource, &error);
  if (!valid)
    return Error(503, "message_format_invalid",
                 "Could not encode thread message");
  return {};
}

ObjectReply ListMessages(const Json& messages, std::string_view raw) {
  auto query = ParseApplicationQuery(raw);
  if (!query.ok()) return query;
  const auto& page = query.value;
  ChatPlanError error;
  auto valid = ValidateAssistant(page, AssistantShape::kMessageQuery, &error);
  if (!valid) return Invalid(valid.message(), error.param);
  return PageAssistantRows(messages, page);
}

ObjectReply PageAssistantRows(const Json& messages, const Json& page) {
  const auto limit = page.GetInt("limit", 20);
  if (limit < 1 || limit > 100) return Invalid("Expected 1 to 100", "limit");
  if (page.Has("after") && page.Has("before"))
    return Invalid("Choose one cursor", "before");
  std::vector<const Json*> rows;
  for (const auto& row : messages.array)
    if (!page.Has("run_id") ||
        (row.Find("run_id")->IsString() &&
         row.GetString("run_id") == page.GetString("run_id")))
      rows.push_back(&row);
  if (page.GetString("order", "desc") == "desc") std::ranges::reverse(rows);
  auto begin = rows.begin(), end = rows.end();
  for (const auto* key : {"after", "before"}) {
    if (!page.Has(key)) continue;
    const auto cursor = page.GetString(key);
    const auto it = std::find_if(begin, end, [&](const auto* row) {
      return row->GetString("id") == cursor;
    });
    if (it == end) return Invalid("Cursor is not in this list", key);
    if (page.Has("after"))
      begin = it + 1;
    else
      end = it;
  }
  const bool more = end - begin > limit;
  if (more) {
    if (page.Has("before"))
      begin = end - limit;
    else
      end = begin + limit;
  }
  auto items = Array();
  for (auto pos = begin; pos != end; ++pos) items.array.push_back(**pos);
  ObjectReply reply;
  reply.value =
      Object({{"object", String("list")},
              {"first_id", begin == end ? Json() : *(*begin)->Find("id")},
              {"last_id", begin == end ? Json() : *(*(end - 1))->Find("id")},
              {"data", std::move(items)},
              {"has_more", Boolean(more)}});
  return reply;
}
}  // namespace q4t::server::assistant_detail
