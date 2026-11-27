#include "q4t/server/responses_state.h"

#include "agent_artifact_internal.h"
#include "agent_session_internal.h"
#include "agent_subagent_internal.h"
#include "chatkit_retry.h"
#include "chatkit_thread_state.h"
#include "agent_workspace_copy.h"

#include <algorithm>
#include <array>
#include <ctime>
#include <unordered_set>

#include "object_protocol.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/batch_state.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_compaction.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/responses_owner.h"
#include "q4t/server/responses_output.h"
#include "q4t/server/responses_web_search.h"
#include "q4t/server/responses_program.h"
#include "q4t/server/responses_mcp.h"
#include "q4t/server/responses_mcp_state.h"
#include "q4t/server/responses_tool_search.h"
#include "q4t/server/schema_graph.h"
#include "q4t/server/tool_result_contracts.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply StateError(std::string message) {
  return Error(503, "response_state_invalid", std::move(message));
}
bool Terminal(const Json& response) {
  const auto status = response.GetString("status");
  return status == "completed" || status == "incomplete" ||
         status == "failed" || status == "cancelled";
}
ObjectRequest Write(ObjectKind kind, const std::string& id,
                    const StateAccess& access, int64_t revision, Json data,
                    bool permanent, bool ephemeral = false) {
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.kind = kind;
  request.id = id;
  request.scope = access.scope;
  request.expected_revision = revision;
  request.data = std::move(data);
  if (permanent || (kind == ObjectKind::kResponse &&
                    (request.data.Has("agent_session_id") || request.data.Has("assistant_thread_id"))))
    request.ttl_seconds = 0;
  if (ephemeral) {
    request.ttl_seconds = 600;
    const auto* response = request.data.Find("response");
    if (response && !Terminal(*response)) {
      const auto* continuation = request.data.Find("continuation");
      const auto until =
          continuation ? continuation->GetInt("execution_deadline_at") : 0;
      // Retain active work through its execution budget plus the terminal
      // polling window. A queued task must not disappear after ten minutes.
      const auto remaining =
          until ? until - std::time(nullptr)
                : std::chrono::ceil<std::chrono::seconds>(
                      access.deadline - std::chrono::steady_clock::now())
                      .count();
      request.ttl_seconds += std::clamp<int64_t>(remaining, 0, 86400);
    }
  }
  return request;
}
Json SavedResponse(ResponseStatePlan& plan, const Json& response,
                   const Json& events) {
  auto result = Object({{"response", response},
                        {"history", plan.history},
                        {"own_input", plan.own_input},
                        {"input_snapshots", plan.input_snapshots},
                        {"tool_contracts", plan.tool_contracts},
                        {"mcp_catalogs", plan.mcp_catalogs},
                        {"web_content", plan.web_content},
                        {"request", plan.request},
                        {"events", events}});
  if (!Terminal(response))
    Set(&result, "terminal_reserve", String(std::string(4096, ' ')));
  Set(&result, "events_retained", Boolean(!events.array.empty()));
  Set(&result, "continuation",
      Object({{"ephemeral", Boolean(plan.ephemeral)},
              {"scope", String(plan.scope)},
              {"owner", plan.execution_owner},
              {"execution_deadline_at", Number(plan.execution_deadline_at)},
              {"rate_domain",
               String(plan.rate_domain == ClientRateDomain::kBatch ? "batch"
                                                                   : "online")},
              {"conversation_id", String(plan.conversation_id)},
              {"conversation_revision", Number(plan.conversation_revision)}}));
  if (!plan.eval_run_id.empty())
    Set(&result, "eval_run_id", String(plan.eval_run_id));
  if (plan.eval_shutdown) Set(&result, "eval_shutdown", Boolean(true));
  if (!plan.chatkit_thread_id.empty()) {
    Set(&result, "chatkit_thread_id", String(plan.chatkit_thread_id));
    if (!plan.chatkit_item_times.IsObject()) plan.chatkit_item_times = Object();
    const auto now = std::time(nullptr);
    if (const auto* items = response.GetArray("output")) {
      for (const auto& item : items->array) {
        const auto type = item.GetString("type");
        if (type != "reasoning" && type != "message") continue;
        const auto id = item.GetString("id");
        if (!id.empty() && !plan.chatkit_item_times.Has(id))
          Set(&plan.chatkit_item_times, id, Number(now));
      }
    }
    Set(&result, "chatkit_item_times", plan.chatkit_item_times);
  }
  if (!plan.assistant_thread_id.empty()) {
    if (!plan.assistant_started_at && response.GetString("status") == "in_progress")
      plan.assistant_started_at = std::time(nullptr);
    if (!plan.assistant_finished_at && Terminal(response))
      plan.assistant_finished_at = std::time(nullptr);
    Set(&result, "assistant_started_at", Number(plan.assistant_started_at));
    Set(&result, "assistant_finished_at", Number(plan.assistant_finished_at));
    if (plan.assistant_prompt_exhausted)
      Set(&result, "assistant_prompt_exhausted", Boolean(true));
    Set(&result, "assistant_thread_id", String(plan.assistant_thread_id));
    Set(&result, "assistant_run_id", String(plan.assistant_run_id));
  }
  if (!plan.agent_session_id.empty()) {
    if (!plan.agent_turn_started_at && response.GetString("status") == "in_progress")
      plan.agent_turn_started_at = std::time(nullptr);
    if (!plan.agent_turn_finished_at && Terminal(response))
      plan.agent_turn_finished_at = std::time(nullptr);
    Set(&result, "agent_session_id", String(plan.agent_session_id));
    if (plan.agent_tree.IsArray()) Set(&result, "agent_tree", plan.agent_tree);
    if (plan.agent_claim_times.IsObject())
      Set(&result, "agent_claim_times", plan.agent_claim_times);
    if (plan.agent_claim_cache_ttls.IsObject())
      Set(&result, "agent_claim_cache_ttls", plan.agent_claim_cache_ttls);
    if (plan.agent_item_times.IsObject())
      Set(&result, "agent_item_times", plan.agent_item_times);
    Set(&result, "agent_turn_started_at", Number(plan.agent_turn_started_at));
    Set(&result, "agent_turn_finished_at", Number(plan.agent_turn_finished_at));
  }
  if (plan.batch_parent)
    Set(&result, "batch_parent", BatchResponseLinkValue(*plan.batch_parent));
  if (plan.multi_agent_beta) Set(&result, "multi_agent_beta", Boolean(true));
  return result;
}
ObjectReply Data(const ObjectReply& stored, std::string_view field) {
  if (!stored.ok()) return stored;
  const auto* data = stored.value.Find("data");
  const auto* value = data ? data->Find(std::string(field)) : nullptr;
  if (!value) return StateError("Stored response is missing required state");
  ObjectReply result;
  result.value = *value;
  return result;
}
ObjectReply Shape(const Json& item, ResponsesItemShape shape,
                  const std::string& path, bool multi_agent = false) {
  ChatPlanError error;
  const auto status = ValidateResponsesItemShape(
      ResponsesToolItemDefaults(item), shape, &error, path, multi_agent);
  if (status) return {};
  auto result = Error(error.http_status, error.code, status.message());
  result.value = Object({{"param", String(error.param)}});
  return result;
}
void Default(Json* item, const std::string& key, Json value) {
  if (!Nonnull(*item, key)) Set(item, key, std::move(value));
}
void CanonicalItem(Json* item) {
  *item = ResponsesToolItemDefaults(std::move(*item));
  if (!item->Has("type") && item->Has("role"))
    Set(item, "type", String("message"));
  const auto type = item->GetString("type");
  if (type == "message") {
    if (const auto* content = item->Find("content"); content->IsString())
      Set(item, "content",
          Array(
              {Object({{"type", String("input_text")}, {"text", *content}})}));
  }
  for (const char* recorded :
       {"message", "function_call", "function_call_output", "custom_tool_call",
        "custom_tool_call_output", "computer_call_output", "tool_search_call",
        "tool_search_output", "shell_call", "shell_call_output"})
    if (type == recorded) Default(item, "status", String("completed"));
  if (type == "tool_search_call" || type == "tool_search_output") {
    // These are client-supplied historical items, not executions by q4t.
    Default(item, "execution", String("client"));
    Default(item, "call_id", Json());
  }
  if (type == "shell_call") {
    Default(item, "environment", Json());
    auto& action = Field(*item, "action");
    Default(&action, "max_output_length", Json());
    Default(&action, "timeout_ms", Json());
  }
  if (type == "shell_call_output") Default(item, "max_output_length", Json());
  if (type == "computer_call_output") {
    const auto* checks = item->Find("acknowledged_safety_checks");
    if (checks && checks->IsNull())
      std::erase_if(item->object, [](const auto& field) {
        return field.first == "acknowledged_safety_checks";
      });
  }
}
ObjectReply BindGeneratedContracts(const Json& snapshot,
                                   ResponseStatePlan* plan) {
  auto history = plan->history;
  const auto* output = snapshot.GetArray("output");
  if (!output) return StateError("Missing generated output");
  history.array.insert(history.array.end(), output->array.begin(),
                       output->array.end());
  auto web = MergeWebContexts(Json(), history, &plan->web_content);
  if (!web.ok()) return web;
  return BindToolResultContracts(plan->request, history, &plan->tool_contracts);
}
bool HasFileParts(const Json& value, size_t depth = 0) {
  if (depth > 16) return false;
  if (value.IsArray()) {
    for (const auto& child : value.array)
      if (HasFileParts(child, depth + 1)) return true;
  } else if (value.IsObject()) {
    const auto type = value.GetString("type");
    if (type == "input_file" || type == "file" || type == "input_image" ||
        type == "image_url")
      return true;
    if (type == "program_output") {
      Json content;
      return !DecodeResponsesProgramOutput(value.GetString("result"),
                                           &content) ||
             HasFileParts(content, depth + 1);
    }
    for (const auto* key : {"content", "output", "results"})
      if (const auto* child = value.Find(key);
          child && HasFileParts(*child, depth + 1))
        return true;
  }
  return false;
}
}  // namespace

ObjectReply CompareStoredItem(const Json& stored, const Json& incoming) {
  std::string left, right;
  auto comparable = stored;
  // Public reads spell out the approval target under both wire names.
  // Only the redundant copy can differ when an SDK reuses a stored item.
  if (stored.GetString("type") == "mcp_approval_response" &&
      incoming.GetString("type") == "mcp_approval_response") {
    const auto* target = stored.Find("approval_request_id");
    const auto* original = stored.Find("request_id");
    const auto* alias = incoming.Find("request_id");
    if (target && target->IsString() &&
        (!original || (original->IsString() && original->str == target->str))) {
      if (alias && alias->IsString() && alias->str == target->str)
        Set(&comparable, "request_id", *target);
      else if (!alias && original)
        std::erase_if(comparable.object, [](const auto& field) {
          return field.first == "request_id";
        });
    }
  }
  if (stored.GetString("type") == "local_shell_call_output" &&
      incoming.GetString("type") == "local_shell_call_output") {
    // An older SDK omits the optional target when reusing an owned item.
    // Never replace an explicit binding with a different call.
    const auto* provided = incoming.Find("call_id");
    if (!provided && stored.GetString("call_id", stored.GetString("id")) ==
                         stored.GetString("id")) {
      std::erase_if(comparable.object,
                    [](const auto& field) { return field.first == "call_id"; });
    } else if (!stored.Has("call_id") && provided && provided->IsString() &&
               provided->str == stored.GetString("id")) {
      Set(&comparable, "call_id", *provided);
    }
  }
  // SDK read views omit search sources unless requested. An omitted source
  // list may recover the original owned context; supplied conflicting fields
  // still fail the ordinary exact identity check.
  if (stored.GetString("type") == "web_search_call" &&
      incoming.GetString("type") == "web_search_call" &&
      incoming.Find("action") && !incoming.Find("action")->Has("sources") &&
      comparable.Has("action"))
    std::erase_if(Field(comparable, "action").object,
                  [](const auto& field) { return field.first == "sources"; });
  auto status = InputValueIdentity(comparable, &left);
  if (status) status = InputValueIdentity(incoming, &right);
  if (!status) return Error(503, "object_identity_capacity", status.message());
  if (left != right)
    return Error(409, "item_identity_conflict",
                 "Existing item has different content");
  return {};
}

ObjectReply ResponsesState::Get(ObjectKind kind, const std::string& id,
                                const StateAccess& access) {
  ObjectRequest request;
  request.operation = ObjectOperation::kGet;
  request.scope = access.scope;
  request.kind = kind;
  request.id = id;
  return objects_.Execute(request, access.deadline, access.cancelled);
}

ObjectReply ResponsesState::ResolveItems(const Json& input,
                                         const StateAccess& access,
                                         size_t maximum, Json* input_snapshots,
                                         Json* tool_contracts,
                                         Json* web_content, bool multi_agent) {
  if (input_snapshots) *input_snapshots = {};
  if (tool_contracts) *tool_contracts = {};
  if (web_content) *web_content = {};
  if (access.scope.empty() || access.scope.size() > 256)
    return StateError("Authenticated state scope is unavailable");
  auto items = Array();
  if (input.IsString())
    items.array.push_back(Object({{"type", String("message")},
                                  {"role", String("user")},
                                  {"content", input}}));
  else if (input.IsArray())
    items = input;
  else if (!input.IsNull())
    return Error(400, "invalid_request", "input requires text or items");
  if (items.array.size() > maximum)
    return Error(400, "input_limit_exceeded", "Input item limit exceeded");
  // Older Python readers get a JSON string for the error in a failed MCP
  // call. Restore that representation only in the prefix of a sealed window.
  // The input shape and authenticated origin are still checked normally.
  bool before_seal = false;
  for (auto it = items.array.rbegin(); it != items.array.rend(); ++it) {
    if (it->GetString("type") == "compaction" &&
        !it->GetString("encrypted_content").empty()) {
      before_seal = true;
    } else if (before_seal && it->GetString("type") == "mcp_call" &&
               it->GetString("status") == "failed") {
      const auto* error = it->Find("error");
      Json parsed;
      if (error && error->IsString() &&
          io::ParseJson(error->str, &parsed, true, kSchemaJsonLimits) &&
          parsed.IsObject())
        Set(&*it, "error", std::move(parsed));
    }
  }
  for (size_t i = 0; i < items.array.size(); ++i) {
    auto valid = Shape(items.array[i], ResponsesItemShape::kInput,
                       "input[" + std::to_string(i) + ']', multi_agent);
    if (!valid.ok()) return valid;
    if (items.array[i].GetString("type") == "compaction_trigger" &&
        i + 1 != items.array.size())
      return Error(400, "invalid_request",
                   "compaction_trigger must be the final item");
  }
  auto private_request = Object({{"input", items}});
  std::vector<ResponsesMcpDefinition> credentials;
  ChatPlanError credential_error;
  auto sanitized = CaptureResponsesMcpCredentials(&private_request, &credentials,
                                                   &credential_error);
  if (!sanitized) {
    auto reply = Error(credential_error.http_status, credential_error.code,
                        sanitized.message());
    reply.value = Object({{"param", String(credential_error.param)}});
    return reply;
  }
  items = std::move(Field(private_request, "input"));
  std::unordered_set<std::string> ids;
  auto snapshots = Array();
  auto contracts = Array();
  auto web = Array();
  for (auto& item : items.array) {
    // The SDK allows an omitted or null discriminator for ItemReference.
    const auto type = item.GetString("type");
    const bool explicit_item = Nonnull(item, "id") &&
                               type != "item_reference" &&
                               !(type.empty() && !item.Has("role"));
    if (type == "item_reference" || (type.empty() && !item.Has("role"))) {
      if (!ValidId(item.GetString("id")))
        return Error(400, "invalid_request", "Invalid item reference");
      auto stored =
          Get(ObjectKind::kConversationItem, item.GetString("id"), access);
      auto reference = Data(stored, "item");
      if (!reference.ok()) return reference;
      item = std::move(reference.value);
      if (tool_contracts && IsResultBoundToolCall(item)) {
        Json own;
        if (const auto* bound =
                stored.value.Find("data")->Find("tool_contracts")) {
          auto reply = MergeToolResultContracts(*bound, Array({item}), &own);
          if (!reply.ok()) return reply;
        }
        auto reply = BindToolResultContracts(Object(), Array({item}), &own);
        if (!reply.ok()) return reply;
        contracts.array.insert(contracts.array.end(), own.array.begin(),
                               own.array.end());
      }
      if (input_snapshots)
        if (const auto* bound =
                stored.value.Find("data")->Find("input_snapshots")) {
          Json own;
          auto reply = MergeInputSnapshots(*bound, Array({item}), &own);
          if (!reply.ok()) return reply;
          snapshots.array.insert(snapshots.array.end(), own.array.begin(),
                                 own.array.end());
        }
    } else {
      CanonicalItem(&item);
      if (!Nonnull(item, "id")) {
        auto id =
            item.GetString("type") == "compaction"
                ? ResponsesCompactionId(item.GetString("encrypted_content"))
                : NewObjectId(ObjectKind::kConversationItem);
        if (id.empty() && item.GetString("type") == "compaction")
          return Error(400, "invalid_encrypted_content",
                       "Invalid authenticated compaction identity");
        if (id.empty()) return StateError("Input item identity unavailable");
        Set(&item, "id", String(std::move(id)));
      }
    }
    const auto id = item.GetString("id");
    if (!ValidId(id) || !ids.insert(id).second)
      return Error(400, "invalid_request", "Invalid or repeated input item ID");
    if (explicit_item && item.GetString("type") == "local_shell_call_output") {
      // SDKs can reuse an owned output without serializing its call_id.
      // Recover its exact binding before storing it in another conversation.
      auto existing = Get(ObjectKind::kConversationItem, id, access);
      if (existing.ok()) {
        auto original = Data(existing, "item");
        if (!original.ok()) return original;
        if (!item.Has("call_id"))
          if (const auto* target = original.value.Find("call_id"))
            Set(&item, "call_id", *target);
        auto same = CompareStoredItem(original.value, item);
        if (!same.ok()) return same;
      } else if (existing.http_status != 404) {
        return existing;
      }
    }
    if (web_content && item.GetString("type") == "web_search_call") {
      auto existing = Get(ObjectKind::kConversationItem, id, access);
      if (existing.ok()) {
        auto original = Data(existing, "item");
        if (!original.ok()) return original;
        auto same = CompareStoredItem(original.value, item);
        if (!same.ok()) return same;
        if (const auto* bound = existing.value.Find("data")->Find("web_content")) {
          Json own;
          auto reply = MergeWebContexts(*bound, Array({item}), &own);
          if (!reply.ok()) return reply;
          web.array.insert(web.array.end(), own.array.begin(), own.array.end());
        }
      } else if (existing.http_status != 404) return existing;
    }
    if (explicit_item && ((input_snapshots && HasFileParts(item)) ||
                          (tool_contracts && IsResultBoundToolCall(item)))) {
      auto existing = Get(ObjectKind::kConversationItem, id, access);
      if (existing.ok()) {
        auto original = Data(existing, "item");
        if (!original.ok()) return original;
        auto same = CompareStoredItem(original.value, item);
        if (!same.ok()) return same;
        if (tool_contracts && IsResultBoundToolCall(item)) {
          Json own;
          if (const auto* bound =
                  existing.value.Find("data")->Find("tool_contracts")) {
            auto reply = MergeToolResultContracts(*bound, Array({item}), &own);
            if (!reply.ok()) return reply;
          }
          auto reply = BindToolResultContracts(Object(), Array({item}), &own);
          if (!reply.ok()) return reply;
          contracts.array.insert(contracts.array.end(), own.array.begin(),
                                 own.array.end());
        }
        if (const auto* bound =
                existing.value.Find("data")->Find("input_snapshots")) {
          Json own;
          auto reply = MergeInputSnapshots(*bound, Array({item}), &own);
          if (!reply.ok()) return reply;
          snapshots.array.insert(snapshots.array.end(), own.array.begin(),
                                 own.array.end());
        }
      } else if (existing.http_status != 404) {
        return existing;
      }
    }
  }
  ObjectReply result;
  result.value = std::move(items);
  if (input_snapshots) *input_snapshots = std::move(snapshots);
  if (tool_contracts) *tool_contracts = std::move(contracts);
  if (web_content) *web_content = std::move(web);
  return result;
}

ObjectReply ResponsesState::Resolve(const ResponsesEnvelope& envelope,
                                    const StateAccess& access,
                                    ResponseStatePlan* plan,
                                    const CancelledAgentHistory* cancelled,
                                    std::string_view agent_session,
                                    std::string_view chatkit_thread,
                                    const chatkit_detail::RetryHistory* retry) {
  *plan = {};
  if (access.scope.empty() || access.scope.size() > 256)
    return StateError("Authenticated state scope is unavailable");
  plan->scope = access.scope;
  plan->id = NewObjectId(ObjectKind::kResponse);
  if (plan->id.empty()) return StateError("Response identity unavailable");
  plan->store = envelope.store || envelope.background;
  plan->ephemeral = envelope.background && !envelope.store;
  if (plan->store) plan->execution_owner = CurrentResponseOwner();
  plan->request = envelope.request;
  plan->multi_agent_beta = envelope.multi_agent_beta;
  plan->history = Array();
  const auto* input = envelope.request.Find("input");
  auto resolved = ResolveItems(input ? *input : Json(), access, 4096,
                               &plan->input_snapshots, &plan->tool_contracts,
                               &plan->web_content, plan->multi_agent_beta);
  if (resolved.http_status == 404 && retry && input &&
      envelope.previous_response_id.empty() && envelope.conversation_id.empty())
    resolved = chatkit_detail::ReadRetryReference(*this, access, chatkit_thread,
                                                  *input, *retry);
  if (!resolved.ok()) return resolved;
  plan->own_input = std::move(resolved.value);
  if (!envelope.previous_response_id.empty()) {
    auto previous =
        Get(ObjectKind::kResponse, envelope.previous_response_id, access);
    if (!previous.ok()) return previous;
    const auto* data = previous.value.Find("data");
    const auto* history = data ? data->GetArray("history") : nullptr;
    const auto* response = data ? data->Find("response") : nullptr;
    if (!history || !response || !response->GetArray("output") ||
        response->GetString("id") != envelope.previous_response_id)
      return StateError("Previous response has inconsistent history");
    if (!Terminal(*response))
      return Error(409, "response_not_completed",
                   "Previous response is still running");
    plan->history = *history;
    if (const auto* bindings = data->Find("input_snapshots")) {
      auto all = plan->history;
      all.array.insert(all.array.end(), plan->own_input.array.begin(),
                       plan->own_input.array.end());
      auto reply = MergeInputSnapshots(*bindings, all, &plan->input_snapshots);
      if (!reply.ok()) return reply;
    }
    if (!agent_session.empty()) {
      if (data->GetString("agent_session_id") != agent_session)
        return StateError("Previous response belongs to another Agent session");
      if (const auto* tree = data->Find("agent_tree")) plan->agent_tree = *tree;
    }
    if (!chatkit_thread.empty() &&
        data->GetString("chatkit_thread_id") != chatkit_thread)
      return StateError("Previous response belongs to another ChatKit thread");
    const auto& output = response->GetArray("output")->array;
    if (!chatkit_thread.empty() &&
        response->GetString("status") == "cancelled") {
      for (const auto& row : output) {
        if (!chatkit_detail::HasPartialAssistantText(row)) continue;
        auto partial = row;
        if (partial.GetString("status") == "in_progress")
          Set(&partial, "status", String("incomplete"));
        plan->history.array.push_back(std::move(partial));
      }
      // Use the terminal response itself so threads reconciled by an older
      // build receive the same context on their next turn.
      auto interruption = ResolveItems(
          Array({Object({{"type", String("message")},
                         {"role", String("developer")},
                         {"content", String("The user cancelled the previous "
                                            "response. Follow the latest user "
                                            "request.")}})}),
          access, 1);
      if (!interruption.ok()) return interruption;
      plan->own_input.array.insert(plan->own_input.array.begin(),
                                   std::move(interruption.value.array.front()));
    } else if (cancelled) {
      auto appended = AppendCancelledAgentHistory(*cancelled, *data, &plan->history);
      if (!appended.ok()) return appended;
    } else if (!agent_session.empty() &&
               agent_detail::RejectedTurnOutput(*response)) {
      // Keep the failed turn inspectable. Two adjacent user messages would
      // otherwise look like one outstanding request to the model.
      auto boundary = ResolveItems(
          Array({Object({{"type", String("message")},
                         {"role", String("developer")},
                         {"content", String("The preceding turn ended with "
                                            "invalid model output. The next "
                                            "user message is a new request. "
                                            "Answer that request.")}})}),
          access, 1);
      if (!boundary.ok()) return boundary;
      plan->history.array.push_back(std::move(boundary.value.array.front()));
    } else if (!chatkit_thread.empty() && plan->own_input.array.size() == 1 &&
               plan->own_input.array.front().GetString("type") == "message" &&
               plan->own_input.array.front().GetString("role") == "user") {
      // A browser may submit another message before returning a client tool
      // result. Keep the original thread and Response intact. Only answered
      // function calls belong in this new turn's model input.
      std::unordered_set<std::string> answered;
      for (const auto& row : output) {
        if (row.GetString("type") == "function_call_output")
          answered.insert(row.GetString("call_id"));
      }
      for (const auto& row : output) {
        const auto call = row.GetString("call_id");
        if (row.GetString("type") == "function_call" && !call.empty() &&
            !answered.contains(call))
          continue;
        plan->history.array.push_back(row);
      }
    } else {
      plan->history.array.insert(plan->history.array.end(), output.begin(), output.end());
    }
    Json inherited;
    if (const auto* contracts = data->Find("tool_contracts")) {
      auto reply =
          MergeToolResultContracts(*contracts, plan->history, &inherited);
      if (!reply.ok()) return reply;
    }
    auto reply = BindToolResultContracts(Object(), plan->history, &inherited);
    if (!reply.ok()) return reply;
    auto all = plan->history;
    all.array.insert(all.array.end(), plan->own_input.array.begin(),
                     plan->own_input.array.end());
    reply = MergeToolResultContracts(inherited, all, &plan->tool_contracts);
    if (!reply.ok()) return reply;
    if (const auto* web = data->Find("web_content")) {
      reply = MergeWebContexts(*web, all, &plan->web_content);
      if (!reply.ok()) return reply;
    }
    // Previous request.instructions is deliberately not consulted.
  } else if (!envelope.conversation_id.empty()) {
    auto conversation =
        Get(ObjectKind::kConversation, envelope.conversation_id, access);
    if (!conversation.ok()) return conversation;
    const auto* data = conversation.value.Find("data");
    const auto* items = data ? data->GetArray("items") : nullptr;
    if (!items || !data->Find("resource"))
      return StateError("Conversation has inconsistent state");
    plan->conversation_id = envelope.conversation_id;
    plan->conversation_revision = conversation.value.GetInt("revision");
    plan->conversation = *data;
    plan->history = *items;
    Json inherited;
    if (const auto* contracts = data->Find("tool_contracts")) {
      auto reply =
          MergeToolResultContracts(*contracts, plan->history, &inherited);
      if (!reply.ok()) return reply;
    }
    auto reply = BindToolResultContracts(Object(), plan->history, &inherited);
    if (!reply.ok()) return reply;
    auto all = plan->history;
    all.array.insert(all.array.end(), plan->own_input.array.begin(),
                     plan->own_input.array.end());
    reply = MergeToolResultContracts(inherited, all, &plan->tool_contracts);
    if (!reply.ok()) return reply;
    if (const auto* web = data->Find("web_content")) {
      reply = MergeWebContexts(*web, all, &plan->web_content);
      if (!reply.ok()) return reply;
    }
    if (const auto* bindings = data->Find("input_snapshots")) {
      auto all = plan->history;
      all.array.insert(all.array.end(), plan->own_input.array.begin(),
                       plan->own_input.array.end());
      auto reply = MergeInputSnapshots(*bindings, all, &plan->input_snapshots);
      if (!reply.ok()) return reply;
    }
  }
  plan->history.array.insert(plan->history.array.end(),
                             plan->own_input.array.begin(),
                             plan->own_input.array.end());
  auto result_contracts = BindToolResultContracts(plan->request, plan->history,
                                                  &plan->tool_contracts);
  if (!result_contracts.ok()) return result_contracts;
  if (plan->history.array.size() > 4096)
    return Error(400, "input_limit_exceeded",
                 "Resolved history exceeds 4096 items");
  ObjectReply result;
  result.value = plan->history;
  return result;
}

ObjectReply ResponsesState::LoadPlan(const std::string& id,
                                     const StateAccess& access,
                                     ResponseStatePlan* plan,
                                     bool restore_conversation) {
  *plan = {};
  auto stored = Get(ObjectKind::kResponse, id, access);
  if (!stored.ok()) return stored;
  const auto* data = stored.value.Find("data");
  const auto* response = data ? data->Find("response") : nullptr;
  if (!response || response->GetString("id") != id ||
      !response->GetArray("output") || !data->GetArray("history") ||
      !data->GetArray("own_input") || !data->GetArray("events") ||
      !data->Find("request") || !data->Find("request")->IsObject() ||
      stored.value.GetInt("revision") < 1)
    return StateError("Stored response cannot restore execution state");
  plan->id = id;
  plan->scope = access.scope;
  if (const auto* eval = data->Find("eval_run_id")) {
    if (!eval->IsString() || !eval->str.starts_with("evalrun_") ||
        !ValidId(eval->str) || !response->GetBool("background"))
      return StateError("Invalid evaluation response binding");
    plan->eval_run_id = eval->str;
  }
  if (const auto* thread = data->Find("chatkit_thread_id")) {
    if (!thread->IsString() || !thread->str.starts_with("cthr_") ||
        !ValidId(thread->str) || !response->GetBool("background"))
      return StateError("Invalid stored ChatKit thread binding");
    plan->chatkit_thread_id = thread->str;
    if (const auto* times = data->Find("chatkit_item_times")) {
      if (!times->IsObject())
        return StateError("Invalid ChatKit item timestamps");
      plan->chatkit_item_times = *times;
    }
  }
  if (data->Has("assistant_thread_id") || data->Has("assistant_run_id")) {
    auto thread = data->GetString("assistant_thread_id");
    auto run = data->GetString("assistant_run_id");
    if (!thread.starts_with("thread_") || !run.starts_with("run_") ||
        !ValidId(thread) || !ValidId(run) || !response->GetBool("background"))
      return StateError("Invalid stored Assistant run binding");
    plan->assistant_thread_id = std::move(thread);
    plan->assistant_run_id = std::move(run);
    plan->assistant_started_at = data->GetInt("assistant_started_at");
    plan->assistant_finished_at = data->GetInt("assistant_finished_at");
  }
  if (const auto* session = data->Find("agent_session_id")) {
    if (!session->IsString() || !session->str.starts_with("agent_session_") ||
        !ValidId(session->str) || !response->GetBool("background"))
      return StateError("Invalid stored Agent session binding");
    plan->agent_session_id = session->str;
    if (const auto* tree = data->Find("agent_tree")) plan->agent_tree = *tree;
    for (auto [field, target] : {std::pair{"agent_item_times", &plan->agent_item_times},
                                 std::pair{"agent_claim_times", &plan->agent_claim_times}}) {
      const auto* times = data->Find(field);
      if (!times) continue;
      if (!times->IsObject() || times->object.size() > ResponsesOutput::kMaxItems)
        return StateError("Invalid stored subagent clock");
      for (const auto& [key, value] : times->object) {
        const Json* at = &value;
        if (target == &plan->agent_claim_times && value.IsObject()) {
          const auto* seq = value.Find("sequence");
          if (value.object.size() != 2 || !seq || !IsInputInteger(*seq) ||
              seq->number < 0 || seq->number > ResponsesOutput::kStorageLimit)
            return StateError("Invalid stored subagent event position");
          at = value.Find("at");
        }
        if (!ValidApplicationId(key) || !at || !IsInputInteger(*at) || at->number <= 0)
          return StateError("Invalid stored subagent clock");
      }
      *target = *times;
    }
    if (const auto* ttls = data->Find("agent_claim_cache_ttls")) {
      if (!agent_detail::ValidSubagentCacheTtls(*ttls, &plan->agent_claim_times))
        return StateError("Invalid subagent cache lifetimes");
      plan->agent_claim_cache_ttls = *ttls;
    }
    plan->agent_turn_started_at = data->GetInt("agent_turn_started_at");
    plan->agent_turn_finished_at = data->GetInt("agent_turn_finished_at");
  }
  if (const auto* selected = data->Find("multi_agent_beta")) {
    if (!selected->IsBool()) return StateError("Invalid stored Beta selector");
    plan->multi_agent_beta = selected->boolean;
  }
  plan->store = true;
  plan->history = *data->Find("history");
  plan->own_input = *data->Find("own_input");
  plan->request = *data->Find("request");
  if (const auto* web = data->Find("web_content")) {
    auto all = plan->history;
    const auto* output = response->GetArray("output");
    if (!output) return StateError("Stored response output is missing");
    all.array.insert(all.array.end(), output->array.begin(), output->array.end());
    auto reply = MergeWebContexts(*web, all, &plan->web_content);
    if (!reply.ok()) return reply;
  }
  if (const auto* catalogs = data->Find("mcp_catalogs"))
    plan->mcp_catalogs = *catalogs;  // Execution owner authenticates each entry.
  if (const auto* contracts = data->Find("tool_contracts")) {
    auto reply = MergeToolResultContracts(*contracts, plan->history,
                                          &plan->tool_contracts);
    if (!reply.ok()) return reply;
  }
  auto result_contracts =
      BindToolResultContracts(Object(), plan->history, &plan->tool_contracts);
  if (!result_contracts.ok()) return result_contracts;
  plan->input_snapshots = Array();
  if (const auto* bindings = data->Find("input_snapshots")) {
    auto reply =
        MergeInputSnapshots(*bindings, plan->history, &plan->input_snapshots);
    if (!reply.ok()) return reply;
  }
  plan->response_revision = stored.value.GetInt("revision");
  plan->finished = Terminal(*response);
  if (const auto* parent = data->Find("batch_parent")) {
    BatchResponseLink link;
    if (!ParseBatchResponseLink(*parent, &link) || link.scope != access.scope ||
        link.response_id != id || !response->GetBool("background"))
      return StateError("Stored Batch response binding is invalid");
    plan->batch_parent = std::move(link);
  }
  if (const auto* continuation = data->Find("continuation")) {
    if (const auto* domain = continuation->Find("rate_domain")) {
      if (!domain->IsString() ||
          (domain->str != "online" && domain->str != "batch"))
        return StateError("Stored response rate domain is invalid");
      plan->rate_domain = domain->str == "batch" ? ClientRateDomain::kBatch
                                                 : ClientRateDomain::kOnline;
    }
    if (const auto* owner = continuation->Find("owner"))
      plan->execution_owner = *owner;
    plan->ephemeral = continuation->GetBool("ephemeral");
    plan->execution_deadline_at = continuation->GetInt("execution_deadline_at");
    plan->conversation_id = continuation->GetString("conversation_id");
    plan->conversation_revision = continuation->GetInt("conversation_revision");
  }
  // The immutable history above remains the generation input. Refresh only
  // the container at its original revision; new input must not leak in.
  if (restore_conversation && !plan->conversation_id.empty() &&
      response->GetString("status") == "queued") {
    auto conversation =
        Get(ObjectKind::kConversation, plan->conversation_id, access);
    if (!conversation.ok()) return conversation;
    if (conversation.value.GetInt("revision") != plan->conversation_revision)
      return Error(409, "conversation_revision_conflict",
                   "Queued response conversation changed");
    const auto* current = conversation.value.Find("data");
    if (!current || !current->GetArray("items"))
      return StateError("Invalid restored conversation");
    plan->conversation = *current;
  }
  ObjectReply result;
  result.value = *data;
  return result;
}

ObjectReply ResponsesState::Interrupt(const std::string& id,
                                      const StateAccess& access,
                                      std::string_view code,
                                      std::string_view message) {
  ResponseStatePlan plan;
  auto loaded = LoadPlan(id, access, &plan);
  if (!loaded.ok()) return loaded;
  auto snapshot = Field(loaded.value, "response");
  if (plan.finished) {
    ObjectReply result;
    result.value = std::move(snapshot);
    return result;
  }
  ChatPlanConfig mcp_config;
  mcp_config.model = snapshot.GetString("model");
  auto recovered = ResponsesMcpState(objects_, mcp_config)
                       .Reconcile(&Field(snapshot, "output"), plan, access);
  if (!recovered.ok() || recovered.commit_unknown) return recovered;
  for (auto& item : Field(snapshot, "output").array) {
    // Only open items need a transition. A committed agent exchange has no
    // status field, and a completed child message remains completed.
    auto* status = item.Find("status");
    if (!status || !status->IsString() ||
        (status->str != "in_progress" && status->str != "calling" &&
         status->str != "searching" && status->str != "interpreting" &&
         status->str != "generating"))
      continue;
    const auto type = item.GetString("type");
    // Apply-patch calls can only be in_progress or completed. A dead owner
    // cannot establish completion. Preserve the last observed item state.
    if (type != "apply_patch_call")
      Set(&item, "status",
          String(type == "image_generation_call" ? "failed" : "incomplete"));
  }
  Set(&snapshot, "status", String("failed"));
  Set(&snapshot, "error",
      Object({{"code", String(std::string(code))},
              {"message", String(std::string(message))}}));
  auto events = Field(loaded.value, "events");
  events.array.push_back(
      Object({{"type", String("response.failed")},
              {"sequence_number", Number(events.array.size())},
              {"response", ResponsesPublicValue(snapshot)}}));
  auto result = Finish(snapshot, events, access, &plan);
  if (!result.commit_unknown && (result.code == "object_too_large" ||
                                 result.code == "object_quota_exceeded"))
    result = FailWithinReservation(snapshot, access, &plan);
  if (result.ok()) return Retrieve(id, access);
  return result;
}

ObjectReply ResponsesState::Begin(const Json& snapshot,
                                  const StateAccess& access,
                                  ResponseStatePlan* plan) {
  return Begin(snapshot, Array(), access, plan);
}
ObjectReply ResponsesState::Begin(const Json& snapshot, const Json& events,
                                  const StateAccess& access,
                                  ResponseStatePlan* plan) {
  if (plan->scope != access.scope)
    return Error(403, "object_scope_mismatch",
                 "Response state belongs to another client");
  if (snapshot.GetString("id") != plan->id || plan->response_revision != 0 ||
      !events.IsArray() ||
      (snapshot.GetString("status") != "queued" &&
       snapshot.GetString("status") != "in_progress"))
    return StateError("Invalid initial response state");
  if (!plan->store) return {};
  if (plan->batch_parent) {
    bool cancelled = false;
    if (plan->batch_parent->response_id != plan->id ||
        !snapshot.GetBool("background"))
      return StateError("Invalid Batch response publication");
    auto parent = CheckBatchResponseParent(objects_, *plan->batch_parent,
                                           access, &cancelled);
    if (!parent.ok()) return parent;
    if (cancelled)
      return Error(499, "batch_cancelled",
                   "Original Batch no longer accepts work");
  }
  auto bindings =
      MergeInputSnapshots(Json(), plan->history, &plan->input_snapshots);
  if (!bindings.ok()) return bindings;
  bindings = BindGeneratedContracts(snapshot, plan);
  if (!bindings.ok()) return bindings;
  auto mutation = Write(ObjectKind::kResponse, plan->id, access, 0,
                        SavedResponse(*plan, snapshot, events),
                        !plan->conversation_id.empty(), plan->ephemeral);
  if (plan->eval_sampling_write) {
    if (plan->agent_session_write || plan->chatkit_thread_write ||
        !plan->assistant_writes.empty() || plan->batch_parent)
      return StateError("Conflicting evaluation row admission");
    const std::array writes{std::move(*plan->eval_sampling_write), std::move(mutation)};
    auto result = objects_.ExecuteBatch(writes, access.deadline, access.cancelled);
    plan->eval_sampling_write.reset();
    if (result.ok()) plan->response_revision = 1;
    return result;
  }
  if (plan->chatkit_thread_write) {
    if (plan->agent_session_write || !plan->assistant_writes.empty())
      return StateError("Conflicting ChatKit thread admission");
    const std::array writes{std::move(*plan->chatkit_thread_write),
                            std::move(mutation)};
    auto result = objects_.ExecuteBatch(writes, access.deadline, access.cancelled);
    plan->chatkit_thread_write.reset();
    if (result.ok()) plan->response_revision = 1;
    return result;
  }
  if (!plan->assistant_thread_id.empty()) {
    if (plan->assistant_writes.empty() || plan->agent_session_write)
      return StateError("Missing Assistant run admission");
    auto writes = std::move(plan->assistant_writes);
    writes.push_back(std::move(mutation));
    auto result = objects_.ExecuteBatch(writes, access.deadline, access.cancelled);
    if (result.ok()) plan->response_revision = 1;
    return result;
  }
  if (plan->agent_session_write) {
    std::vector<ObjectRequest> writes;
    writes.reserve(2 + plan->agent_environment_write.has_value() +
                   plan->automation_delivery_write.has_value());
    writes.push_back(std::move(mutation));
    writes.push_back(std::move(*plan->agent_session_write));
    plan->agent_session_write.reset();
    if (plan->agent_environment_write) {
      writes.push_back(std::move(*plan->agent_environment_write));
      plan->agent_environment_write.reset();
    }
    if (plan->automation_delivery_write) {
      writes.push_back(std::move(*plan->automation_delivery_write));
      plan->automation_delivery_write.reset();
    }
    auto result = objects_.ExecuteBatch(writes, access.deadline, access.cancelled);
    if (plan->workspace_copies) {
      plan->workspace_copies->Settle(result);
      plan->workspace_copies.reset();
    }
    if (result.ok()) plan->response_revision = 1;
    return result;
  }
  if (!plan->agent_session_id.empty())
    return StateError("Agent session creation requires an atomic receipt");
  auto written = objects_.Execute(mutation, access.deadline, access.cancelled);
  if (written.ok()) plan->response_revision = written.value.GetInt("revision");
  return written;
}

ObjectReply ResponsesState::Checkpoint(const Json& snapshot, const Json& events,
                                       const StateAccess& access,
                                       ResponseStatePlan* plan,
                                       const ObjectRequest* automation) {
  if (plan->scope != access.scope)
    return Error(403, "object_scope_mismatch",
                 "Response belongs to another client");
  if (plan->finished || snapshot.GetString("id") != plan->id ||
      !events.IsArray() || !snapshot.GetArray("output") ||
      (snapshot.GetString("status") != "queued" &&
       snapshot.GetString("status") != "in_progress") ||
      (plan->store && plan->response_revision <= 0))
    return StateError("Invalid response checkpoint");
  if (automation) {
    const auto* row = automation->data.Find("resource");
    const auto* creator = row ? row->Find("created_by") : nullptr;
    const auto* target = row ? row->Find("target") : nullptr;
    if (!plan->store || plan->agent_session_id.empty() ||
        automation->kind != ObjectKind::kAutomation ||
        automation->scope != access.scope || !creator || !target ||
        row->GetString("id") != automation->id ||
        creator->GetString("type") != "agent" ||
        creator->GetString("session_id") != plan->agent_session_id ||
        target->GetString("type") != "existing_session" ||
        target->GetString("session_id") != plan->agent_session_id ||
        (automation->operation != ObjectOperation::kPut &&
         !(automation->operation == ObjectOperation::kErase &&
           automation->expected_revision > 0)))
      return StateError("Invalid session automation checkpoint");
  }
  if (!plan->store) return {};
  auto bindings =
      MergeInputSnapshots(Json(), plan->history, &plan->input_snapshots);
  if (!bindings.ok()) return bindings;
  bindings = BindGeneratedContracts(snapshot, plan);
  if (!bindings.ok()) return bindings;
  auto response =
      Write(ObjectKind::kResponse, plan->id, access, plan->response_revision,
            SavedResponse(*plan, snapshot, events),
            !plan->conversation_id.empty(), plan->ephemeral);
  if (automation) {
    const std::array writes{std::move(response), *automation};
    auto saved =
        objects_.ExecuteBatch(writes, access.deadline, access.cancelled);
    if (saved.ok()) ++plan->response_revision;
    return saved;
  }
  auto written = objects_.Execute(response, access.deadline, access.cancelled);
  if (written.ok()) plan->response_revision = written.value.GetInt("revision");
  return written;
}

ObjectReply ResponsesState::Finish(const Json& snapshot, const Json& events,
                                   const StateAccess& access,
                                   ResponseStatePlan* plan) {
  if (plan->scope != access.scope)
    return Error(403, "object_scope_mismatch",
                 "Response state belongs to another client");
  if (plan->finished || snapshot.GetString("id") != plan->id ||
      !Terminal(snapshot) || !snapshot.GetArray("output") ||
      !events.IsArray() || (plan->store && plan->response_revision <= 0))
    return StateError("Invalid terminal response state");
  for (const auto& item : snapshot.GetArray("output")->array)
    if (!Shape(ResponsesPublicValue(item), ResponsesItemShape::kOutput,
               "output", plan->multi_agent_beta)
             .ok())
      return StateError("Generated response item violates the output contract");
  auto bindings =
      MergeInputSnapshots(Json(), plan->history, &plan->input_snapshots);
  if (!bindings.ok()) return bindings;
  bindings = BindGeneratedContracts(snapshot, plan);
  if (!bindings.ok()) return bindings;
  if (!plan->store && plan->conversation_id.empty()) {
    plan->finished = true;
    return {};
  }
  std::vector<ObjectRequest> mutations;
  const bool permanent = !plan->conversation_id.empty();
  if (plan->store)
    mutations.push_back(Write(
        ObjectKind::kResponse, plan->id, access, plan->response_revision,
        SavedResponse(*plan, snapshot, events), permanent, plan->ephemeral));
  auto items = plan->own_input;
  const auto& output = snapshot.GetArray("output")->array;
  items.array.insert(items.array.end(), output.begin(), output.end());
  const bool publish = snapshot.GetString("status") == "completed" ||
                       snapshot.GetString("status") == "incomplete";
  const bool append = permanent && publish;
  if (!publish) items.array.clear();
  if (!plan->store && !append) {
    plan->finished = true;
    return {};
  }
  if (append) {
    auto conversation = plan->conversation;
    auto* previous = &Field(conversation, "items");
    if (previous->array.size() + items.array.size() > 4096)
      return Error(413, "conversation_item_limit",
                   "Conversation item limit exceeded");
    std::unordered_set<std::string> membership;
    for (const auto& item : previous->array)
      membership.insert(item.GetString("id"));
    for (const auto& item : items.array)
      if (!membership.insert(item.GetString("id")).second)
        return Error(409, "item_already_in_conversation",
                     "Item already belongs to this conversation");
    previous->array.insert(previous->array.end(), items.array.begin(),
                           items.array.end());
    auto bound = conversation.Find("input_snapshots")
                     ? *conversation.Find("input_snapshots")
                     : Json();
    bindings = MergeInputSnapshots(plan->input_snapshots, *previous, &bound);
    if (!bindings.ok()) return bindings;
    Set(&conversation, "input_snapshots", std::move(bound));
    Json contracts;
    bindings =
        MergeToolResultContracts(plan->tool_contracts, *previous, &contracts);
    if (!bindings.ok()) return bindings;
    Set(&conversation, "tool_contracts", std::move(contracts));
    auto web = conversation.Find("web_content") ? *conversation.Find("web_content") : Json();
    bindings = MergeWebContexts(plan->web_content, *previous, &web);
    if (!bindings.ok()) return bindings;
    Set(&conversation, "web_content", std::move(web));
    mutations.push_back(Write(ObjectKind::kConversation, plan->conversation_id,
                              access, plan->conversation_revision,
                              std::move(conversation), true));
  }
  // A previously appended conversation item may be materialized for the first
  // time by this generation. Retain that same result for future item-reference
  // reads without appending the historical item to the conversation again.
  if (publish) {
    std::unordered_set<std::string> published;
    for (const auto& item : items.array) published.insert(item.GetString("id"));
    for (const auto& binding : plan->input_snapshots.array) {
      const auto id = binding.GetString("item_id");
      if (!published.insert(id).second) continue;
      for (const auto& item : plan->history.array)
        if (item.GetString("id") == id) {
          items.array.push_back(item);
          break;
        }
    }
  }
  std::unordered_set<std::string> seen;
  for (const auto& item : items.array) {
    const auto id = item.GetString("id");
    if (!ValidId(id) || !seen.insert(id).second)
      return StateError("Generated item identity invalid or repeated");
    auto existing = Get(ObjectKind::kConversationItem, id, access);
    Json item_web;
    bindings = MergeWebContexts(plan->web_content, Array({item}), &item_web);
    if (!bindings.ok()) return bindings;
    Json item_bindings;
    bindings = SelectInputSnapshots(plan->input_snapshots, Array({item}),
                                    &item_bindings);
    if (!bindings.ok()) return bindings;
    int64_t revision = 0;
    bool keep = permanent;
    bool temporary = plan->ephemeral && !keep;
    if (existing.ok()) {
      auto original = Data(existing, "item");
      if (!original.ok()) return original;
      auto identical = CompareStoredItem(original.value, item);
      if (!identical.ok()) return identical;
      if (const auto* bound =
              existing.value.Find("data")->Find("input_snapshots")) {
        bindings = MergeInputSnapshots(*bound, Array({item}), &item_bindings);
        if (!bindings.ok()) return bindings;
      }
      if (const auto* web = existing.value.Find("data")->Find("web_content")) {
        bindings = MergeWebContexts(*web, Array({item}), &item_web);
        if (!bindings.ok()) return bindings;
      }
      revision = existing.value.GetInt("revision");
      keep = keep || existing.value.GetInt("expires_at", -1) == 0;
      temporary = false;  // Never shorten another retained item's lifetime.
    } else if (existing.http_status != 404) {
      return existing;
    }
    Json contracts;
    bindings = MergeToolResultContracts(plan->tool_contracts, Array({item}),
                                        &contracts);
    if (!bindings.ok()) return bindings;
    mutations.push_back(Write(ObjectKind::kConversationItem, id, access,
                              revision,
                              Object({{"item", item},
                                      {"input_snapshots", item_bindings},
                                      {"tool_contracts", contracts},
                                      {"web_content", item_web}}),
                              keep, temporary));
  }
  agent_detail::TurnArtifacts artifacts(objects_, access);
  if (!plan->agent_session_id.empty() &&
      snapshot.GetString("status") == "completed") {
    auto capture = artifacts.Prepare(plan->agent_session_id, plan->id, &mutations);
    if (!capture.ok()) return capture;
  }
  auto written =
      objects_.ExecuteBatch(mutations, access.deadline, access.cancelled);
  artifacts.Settle(written);
  if (written.ok()) {
    if (plan->store) ++plan->response_revision;
    if (append) ++plan->conversation_revision;
    plan->finished = true;
  }
  return written;
}

ObjectReply ResponsesState::FailWithinReservation(const Json& snapshot,
                                                  const StateAccess& access,
                                                  ResponseStatePlan* plan) {
  if (snapshot.GetString("status") != "failed" &&
      snapshot.GetString("status") != "cancelled")
    return StateError("Reserved terminal space cannot publish success");
  auto minimal = Object();
  for (const auto& [key, value] : snapshot.object)
    if (key != "output" && key != "error") Set(&minimal, key, value);
  Set(&minimal, "output", Array());
  if (snapshot.GetString("status") == "cancelled") {
    Set(&minimal, "error", Json());
  } else {
    const auto* error = snapshot.Find("error");
    const auto code =
        error ? error->GetString("code") : "response_storage_exhausted";
    const auto message =
        error ? error->GetString("message") : "Response state full";
    Set(&minimal, "error",
        Object(
            {{"code", String(detail::NormalizeUtf8(code.substr(0, 128)))},
             {"message", String(detail::NormalizeUtf8(message.substr(0, 256)) +
                                "; partial output and events could not be "
                                "retained")}}));
  }
  return Finish(minimal, Array(), access, plan);
}

ObjectReply ResponsesState::Retrieve(std::string id, const StateAccess& access,
                                     Json* chatkit_item_times) {
  auto stored = Get(ObjectKind::kResponse, id, access);
  auto result = Data(stored, "response");
  if (!result.ok()) return result;
  if (chatkit_item_times) {
    const auto* times = stored.value.Find("data")->Find("chatkit_item_times");
    *chatkit_item_times = times ? *times : Json{};
  }
  result.value = ResponsesPublicValue(std::move(result.value));
  return result;
}
ObjectReply ResponsesState::ReadRecord(const std::string& id,
                                       const StateAccess& access) {
  auto result = Get(ObjectKind::kResponse, id, access);
  if (!result.ok()) return result;
  const auto* data = result.value.Find("data");
  const auto* response = data ? data->Find("response") : nullptr;
  if (!response || response->GetString("id") != id ||
      !response->GetArray("output") || !data->GetArray("history") ||
      !data->GetArray("events"))
    return StateError("Stored response is missing required state");
  auto record = *data;
  result.value = std::move(record);
  return result;
}
ObjectReply ResponsesState::Erase(std::string id, const StateAccess& access) {
  auto existing = Get(ObjectKind::kResponse, id, access);
  auto response = Data(existing, "response");
  if (!response.ok()) return response;
  if (!Terminal(response.value))
    return Error(409, "response_not_completed",
                 "Cancel an active response before deletion");
  if (const auto* thread = existing.value.Find("data")->Find("chatkit_thread_id")) {
    if (!thread->IsString() || !thread->str.starts_with("cthr_") || !ValidId(thread->str))
      return StateError("Invalid stored ChatKit thread binding");
    auto row = Get(ObjectKind::kChatKitThread, thread->str, access);
    if (!row.ok() && row.http_status != 404) return row;
    // Each new turn has a fresh response ID. Once a thread has advanced past
    // this ID it can never reacquire an older response as pending or last.
    if (row.ok()) {
      const auto& data = *row.value.Find("data");
      if (data.GetString("pending_response") == id || data.GetString("last_response") == id)
        return Error(409, "chatkit_thread_owns_response", "The thread still uses this response");
    }
  }
  if (existing.value.Find("data")->Has("eval_run_id"))
    return Error(409, "eval_run_owns_response",
                 "The evaluation owns this model sample");
  if (existing.value.Find("data")->Has("assistant_thread_id"))
    return Error(409, "assistant_thread_owns_response",
                 "Delete the owning Assistant thread to release this run");
  if (existing.value.Find("data")->Has("agent_session_id"))
    return Error(409, "agent_session_owns_response",
                 "Delete the owning Agent session to release its turn");
  if (const auto* parent = existing.value.Find("data")->Find("batch_parent")) {
    BatchResponseLink link;
    if (!ParseBatchResponseLink(*parent, &link) || link.scope != access.scope ||
        link.response_id != id)
      return StateError("Stored Batch response binding is invalid");
    // The parent prefix only advances. Once it passes this row, a later
    // erase cannot race a new dependency on this same execution identity.
    auto batch = BatchState(objects_).Load(
        {access.scope, access.deadline, access.cancelled}, link.batch_id);
    if (!batch.ok() && batch.http_status != 404) return batch;
    if (batch.ok()) {
      const auto& data = *batch.value.Find("data");
      if (!BatchTerminal(data.Find("batch")->GetString("status")) &&
          data.GetInt("next_row") <= static_cast<int64_t>(link.index))
        return Error(409, "response_batch_result_pending",
                     "The original Batch has not received this result yet");
    }
  }
  ObjectRequest request;
  request.operation = ObjectOperation::kErase;
  request.kind = ObjectKind::kResponse;
  request.scope = access.scope;
  request.id = std::move(id);
  request.expected_revision = existing.value.GetInt("revision");
  return objects_.Execute(request, access.deadline, access.cancelled);
}
}  // namespace q4t::server
