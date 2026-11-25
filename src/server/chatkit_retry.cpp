#include "chatkit_retry.h"

#include <algorithm>
#include <unordered_set>

#include "chatkit_attachments.h"
#include "chatkit_thread_state.h"
#include "object_protocol.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/responses_web_search.h"
#include "q4t/server/tool_result_contracts.h"

namespace q4t::server::chatkit_detail {
namespace {
using namespace object_detail;

ObjectReply Corrupt() {
  return Error(503, "chatkit_record_invalid", "Inconsistent retry history");
}

bool IsUser(const Json& item) {
  return item.GetString("type") == "message" &&
         item.GetString("role") == "user";
}
}  // namespace

ObjectReply SelectRetryHistory(const Json& record, std::string_view item_id,
                               Json* thread_data, Json* user,
                               RetryHistory* history) {
  auto& visible = Field(*thread_data, "items");
  const auto* saved = record.GetArray("history");
  const auto* response = record.Find("response");
  const auto* thread = thread_data->Find("thread");
  if (!visible.IsArray() || !saved || !response || !thread ||
      record.GetString("chatkit_thread_id") != thread->GetString("id") ||
      response->GetString("id") != thread_data->GetString("last_response"))
    return Corrupt();

  const auto target = std::ranges::find_if(visible.array, [&](const Json& row) {
    return row.GetString("id") == item_id;
  });
  if (target == visible.array.end()) return {};
  const auto browser_user = [](const Json& item) {
    return item.GetString("type") == "chatkit.user_message";
  };
  if (!browser_user(*target)) {
    auto error = Error(400, "invalid_request", "Retry requires a user message");
    error.value = Object({{"param", String("item_id")}});
    return error;
  }
  // Released threads use a separate browser ID for the same user input.
  // The ordered user turns are shared; tool results are separate model items.
  const auto ordinal =
      std::count_if(visible.array.begin(), target, browser_user);
  if (std::ranges::count_if(visible.array, browser_user) !=
      std::ranges::count_if(saved->array, IsUser))
    return Corrupt();
  auto end = saved->array.begin();
  for (auto index = ordinal; end != saved->array.end(); ++end) {
    if (IsUser(*end) && index-- == 0) break;
  }
  if (end == saved->array.end() || end->GetString("id").empty())
    return Corrupt();
  const auto browser = ClientItem(*target);
  const auto* attachments = browser.GetArray("attachments");
  const auto* parts = browser.GetArray("content");
  if (!parts || !attachments || !end->GetArray("content")) return Corrupt();
  auto model = Array(), public_parts = Array();
  const auto& original = end->GetArray("content")->array;
  if (attachments->array.size() > original.size()) return Corrupt();
  for (const auto& attachment : attachments->array) {
    const auto* id = attachment.Find("id");
    if (!id || !id->IsString()) return Corrupt();
    // Early releases forwarded every generic browser file as a document.
    // Keep its stored modality and verify the entire canonical message below.
    if (attachment.GetString("type") == "file" &&
        original[model.array.size()].GetString("type") == "input_file")
      model.array.push_back(
          Object({{"type", String("input_file")}, {"file_id", *id}}));
    else
      model.array.push_back(AttachmentModelInput(attachment));
  }
  if (const auto* quote = browser.Find("quoted_text"))
    model.array.push_back(
        Object({{"type", String("input_text")}, {"text", *quote}}));
  auto projected = AppendUserContent(*parts, &public_parts, &model);
  std::string expected, actual;
  if (!projected.ok() || !InputValueIdentity(model, &expected) ||
      !InputValueIdentity(*end->GetArray("content"), &actual) ||
      actual != expected)
    return Corrupt();
  history->items = Array();
  history->items.array.assign(saved->array.begin(), end + 1);
  const Json empty;
  const auto field = [&](const char* name) -> const Json& {
    const auto* value = record.Find(name);
    return value ? *value : empty;
  };
  auto valid = SelectInputSnapshots(field("input_snapshots"), history->items,
                                    &history->snapshots);
  if (!valid.ok()) return valid;
  valid = MergeToolResultContracts(field("tool_contracts"), history->items,
                                   &history->contracts);
  if (!valid.ok()) return valid;
  valid = MergeWebContexts(field("web_content"), history->items,
                           &history->web_content);
  if (!valid.ok()) return valid;

  *user = *target;
  visible.array.erase(target + 1, visible.array.end());
  if (thread_data->Has("feedback")) {
    auto& feedback = Field(*thread_data, "feedback");
    if (!feedback.IsObject()) return Corrupt();
    std::unordered_set<std::string> retained;
    for (const auto& item : visible.array)
      retained.insert(item.GetString("id"));
    std::erase_if(feedback.object, [&](const auto& entry) {
      return !retained.contains(entry.first);
    });
  }
  return {};
}

ObjectReply ApplyRetryHistory(const RetryHistory& history,
                              ResponseStatePlan* plan) {
  if (!history.items.IsArray() || history.items.array.empty() ||
      history.items.array.size() > 4096 || plan->history.array.size() != 1 ||
      !plan->conversation_id.empty())
    return Corrupt();
  std::string saved, admitted;
  if (!InputValueIdentity(history.items.array.back(), &saved) ||
      !InputValueIdentity(plan->history.array.front(), &admitted) ||
      saved != admitted)
    return Corrupt();
  plan->history = history.items;
  auto valid = MergeInputSnapshots(history.snapshots, plan->history,
                                   &plan->input_snapshots);
  if (!valid.ok()) return valid;
  valid = MergeToolResultContracts(history.contracts, plan->history,
                                   &plan->tool_contracts);
  if (!valid.ok()) return valid;
  return MergeWebContexts(history.web_content, plan->history,
                          &plan->web_content);
}
}  // namespace q4t::server::chatkit_detail
