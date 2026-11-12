#include "q4t/server/responses_program.h"

#include <algorithm>
#include <array>
#include <ctime>
#include <unordered_map>
#include <unordered_set>

#include "object_protocol.h"
#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/program_worker.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/tool_result_schema.h"
#include "q4t/server/tool_result_contracts.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kCodeBytes = 65536;
constexpr size_t kStateBytes = 1024 * 1024;
constexpr size_t kHistoryBytes = 8 * 1024 * 1024;
constexpr size_t kCalls = 64;
constexpr int64_t kLifetime = 30 * 24 * 3600;
struct Plaintext {
  std::string bytes;
  ~Plaintext() { CryptoWipe(&bytes); }
};
ObjectReply Invalid(std::string message, std::string param = "input") {
  auto reply = Error(400, "invalid_program_state", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply Active(const StateAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Program request cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "Program request expired");
  return {};
}
StateCipherBinding Binding(const ChatPlanConfig& config, const Json& item) {
  return {config.model,
          Digest(std::string(identity::kTemplateReference) + ':' +
                 identity::kRenderer),
          "responses.program.v1", item.GetString("id")};
}
Json PlainProgram(Json program) {
  std::erase_if(program.object,
                [](const auto& field) { return field.first == "fingerprint"; });
  return program;
}
bool Same(const Json& a, const Json& b) {
  std::string left, right;
  return InputValueIdentity(a, &left) && InputValueIdentity(b, &right) &&
         left == right;
}
Json Caller(const Json& program) {
  return Object(
      {{"type", String("program")}, {"caller_id", *program.Find("call_id")}});
}
std::string CallId(const Json& program, size_t index) {
  return "call_p_" +
         Digest(program.GetString("call_id") + ':' + std::to_string(index))
             .substr(0, 56);
}
const Json* FindTool(const Json& tools, std::string_view key) {
  for (const auto& tool : tools.array)
    if (tool.GetString("key") == key) return &tool;
  return nullptr;
}
ObjectReply Shape(const Json& item, const std::string& path) {
  auto projected = item;
  const auto type = item.GetString("type");
  if (type == "custom_tool_call" || type == "custom_tool_call_output") {
    // Resolution retains the Item lifecycle; the SDK's public custom input
    // shapes omit these fields. Validate lifecycle before projecting it out.
    if (const auto* status = item.Find("status");
        status && (!status->IsString() ||
                   (status->str != "completed" && status->str != "incomplete" &&
                    status->str != "in_progress")))
      return Invalid("Invalid custom tool lifecycle", path + ".status");
    if (const auto* origin = item.Find("created_by");
        origin && !origin->IsNull() && !origin->IsString())
      return Invalid("Invalid custom tool origin", path + ".created_by");
    std::erase_if(projected.object, [](const auto& field) {
      return field.first == "status" || field.first == "created_by";
    });
  }
  ChatPlanError error;
  auto status = ValidateResponsesItemShape(
      projected, ResponsesItemShape::kInput, &error, path);
  return status ? ObjectReply{} : Invalid(status.message(), error.param);
}
Json Child(const Json& program, const Json& tool, const Json& call,
           size_t index) {
  const bool custom = tool.GetString("type") == "custom";
  const auto call_id = CallId(program, index);
  auto result =
      Object({{"id", String((custom ? "ctc_" : "fc_") +
                            Digest(call_id).substr(0, 56))},
              {"type", String(custom ? "custom_tool_call" : "function_call")},
              {"call_id", String(call_id)},
              {"name", *tool.Find("name")},
              {custom ? "input" : "arguments", *call.Find("arguments")},
              {"status", String("completed")},
              {"caller", Caller(program)}});
  if (!tool.GetString("namespace").empty())
    Set(&result, "namespace", *tool.Find("namespace"));
  if (tool.Find("definition")->GetBool("async"))
    Set(&result, "async", Boolean(true));
  return result;
}
ObjectReply CallsContract(const ResponsesProgramState& program,
                          const Json& calls, const StateAccess& access) {
  auto checks = Array();
  for (size_t i = 0; i < calls.array.size(); ++i) {
    const auto& call = calls.array[i];
    const auto* tool = FindTool(program.tools, call.GetString("key"));
    if (!tool) return Invalid("Program called a tool outside its authority");
    const auto& definition = *tool->Find("definition");
    const auto param = "program.calls[" + std::to_string(i) + ']';
    if (tool->GetString("type") == "function") {
      Json arguments;
      if (!io::ParseJson(call.GetString("arguments"), &arguments, true,
                         kSchemaJsonLimits) ||
          !arguments.IsObject())
        return Invalid("Program function arguments must be a JSON object",
                       param);
      if (const auto* schema = definition.Find("parameters");
          schema && !schema->IsNull())
        checks.array.push_back(Object({{"schema", *schema},
                                       {"output", *call.Find("arguments")},
                                       {"param", String(param)}}));
    } else {
      checks.array.push_back(Object({{"custom", definition},
                                     {"output", *call.Find("arguments")},
                                     {"param", String(param)}}));
    }
  }
  return ValidateToolResultSchemas(
      checks, {access.scope, access.deadline, access.cancelled});
}
}  // namespace

Status ValidateResponsesAllowedCallers(const Json& definition,
                                       ChatPlanError* error,
                                       const std::string& path) {
  const auto* callers = definition.Find("allowed_callers");
  if (!callers || callers->IsNull()) return {};
  std::unordered_set<std::string> seen;
  if (callers->IsArray() && !callers->array.empty() &&
      callers->array.size() <= 2 &&
      std::all_of(
          callers->array.begin(), callers->array.end(), [&](const auto& value) {
            return value.IsString() &&
                   (value.str == "direct" || value.str == "programmatic") &&
                   seen.insert(value.str).second;
          }))
    return {};
  *error = {"invalid_request", path + ".allowed_callers", 400};
  return Status::Fail("Expected distinct direct/programmatic callers");
}

bool ResponsesToolAllows(const Json& definition, std::string_view caller) {
  ChatPlanError error;
  if ((caller != "direct" && caller != "programmatic") ||
      !ValidateResponsesAllowedCallers(definition, &error, "tool"))
    return false;
  const auto* callers = definition.GetArray("allowed_callers");
  if (!callers) return caller == "direct";
  return std::any_of(callers->array.begin(), callers->array.end(),
                     [&](const auto& value) { return value.str == caller; });
}

ObjectReply CreateResponsesProgram(StateCipher& cipher, std::string code,
                                   const Json& active_tools,
                                   const ChatPlanConfig& config,
                                   const StateAccess& access, Json* program,
                                   bool parallel) {
  auto active = Active(access);
  if (!active.ok()) return active;
  if (!program || !active_tools.IsArray() || active_tools.array.size() > 128 ||
      code.size() > kCodeBytes || code.find('\0') != code.npos)
    return Invalid("Invalid program or active catalog", "tools");
  std::vector<ResponsesToolIdentity> identities;
  ChatPlanError error;
  auto status = PlanResponsesToolIdentities(Object({{"tools", active_tools}}),
                                            &identities, &error);
  if (!status) return Invalid(status.message(), error.param);
  auto tools = Array();
  bool enabled = false;
  const auto add = [&](const Json& tool, const std::string& space) -> Status {
    auto checked = ValidateResponsesAllowedCallers(tool, &error, "tools");
    if (!checked) return checked;
    if ((tool.GetString("type") == "function" ||
         tool.GetString("type") == "custom") &&
        ResponsesToolAllows(tool, "programmatic"))
      tools.array.push_back(Object(
          {{"key", String(ResponsesToolModelName(space, tool.GetString("name"),
                                                 tool.GetString("type")))},
           {"type", *tool.Find("type")},
           {"name", *tool.Find("name")},
           {"namespace", String(space)},
           {"definition", tool}}));
    return {};
  };
  for (const auto& tool : active_tools.array) {
    if (tool.GetString("type") == "programmatic_tool_calling") enabled = true;
    if (tool.GetString("type") == "namespace") {
      for (const auto& child : tool.GetArray("tools")->array) {
        status = add(child, tool.GetString("name"));
        if (!status) return Invalid(status.message(), error.param);
      }
    } else {
      status = add(tool, "");
      if (!status) return Invalid(status.message(), error.param);
    }
  }
  if (!enabled) return Invalid("Program tool is not enabled", "tools");
  auto item = Object({{"type", String("program")},
                      {"id", String(detail::NewRequestId("prog_"))},
                      {"call_id", String(detail::NewRequestId("call_prog_"))},
                      {"code", String(std::move(code))}});
  std::array<uint8_t, 4> entropy;
  if (CryptoRandom(entropy) != CryptoResult::kOk)
    return Error(503, "state_crypto_unavailable",
                 "Program entropy unavailable");
  uint64_t seed = 0;
  for (const auto byte : entropy) seed = (seed << 8) | byte;
  const auto now = std::time(nullptr);
  Plaintext plaintext;
  plaintext.bytes =
      OutputJson(Object({{"version", Number(1)},
                         {"program", item},
                         {"tools", std::move(tools)},
                         {"seed", Number(seed)},
                         {"parallel", Boolean(parallel)},
                         {"epoch_ms", Number(now * 1000)},
                         {"expires_at", Number(now + kLifetime)}}));
  if (plaintext.bytes.size() > kStateBytes)
    return Error(503, "program_state_capacity", "Program authority too large");
  std::string sealed;
  auto result =
      cipher.Seal(plaintext.bytes, Binding(config, item), access, &sealed);
  if (result.commit_unknown)
    return Error(503, "state_crypto_commit_unknown",
                 "Key accounting receipt unavailable; no program issued");
  if (!result.ok()) return result;
  Set(&item, "fingerprint", String(std::move(sealed)));
  *program = std::move(item);
  return {};
}

ObjectReply OpenResponsesPrograms(
    StateCipher& cipher, const Json& history, const ChatPlanConfig& config,
    const StateAccess& access, std::vector<ResponsesProgramState>* programs) {
  if (!programs || !history.IsArray() || history.array.size() > 4096 ||
      OutputJson(history).size() > kHistoryBytes)
    return Invalid("Program history exceeds working set");
  std::vector<ResponsesProgramState> states;
  std::unordered_map<std::string, size_t> owners;
  std::unordered_map<std::string, std::pair<size_t, size_t>> calls;
  std::unordered_set<std::string> returned;
  auto checks = Array();
  size_t decoded_bytes = 0;
  for (size_t i = 0; i < history.array.size(); ++i) {
    auto active = Active(access);
    if (!active.ok()) return active;
    const auto& item = history.array[i];
    const auto type = item.GetString("type");
    const auto path = "input[" + std::to_string(i) + ']';
    if (type == "program") {
      auto shape = Shape(item, path);
      if (!shape.ok()) return shape;
      if (states.size() >= kCalls || !ValidId(item.GetString("call_id")) ||
          item.GetString("code").size() > kCodeBytes ||
          !owners.emplace(item.GetString("call_id"), states.size()).second)
        return Invalid("Invalid or duplicate program identity", path);
      Plaintext plaintext;
      auto opened =
          cipher.Open(item.GetString("fingerprint"), Binding(config, item),
                      access, &plaintext.bytes);
      if (!opened.ok()) return opened;
      if (plaintext.bytes.size() > kStateBytes ||
          plaintext.bytes.size() > kHistoryBytes - decoded_bytes)
        return Error(503, "program_state_capacity", "Program state too large");
      decoded_bytes += plaintext.bytes.size();
      Json payload;
      auto limits = kSchemaJsonLimits;
      limits.max_string_bytes = kStateBytes;
      if (!io::ParseJson(plaintext.bytes, &payload, true, limits) ||
          payload.GetInt("version") != 1 || !payload.Find("program") ||
          !Same(*payload.Find("program"), PlainProgram(item)) ||
          !payload.GetArray("tools") || !payload.Has("seed") ||
          !payload.Has("epoch_ms") || !payload.Has("expires_at"))
        return Invalid("Program differs from authenticated origin", path);
      ResponsesProgramState state;
      state.program = item;
      state.tools = *payload.GetArray("tools");
      state.input_index = i;
      state.worker_input =
          Object({{"code", *item.Find("code")},
                  {"tools", state.tools},
                  {"seed", *payload.Find("seed")},
                  {"epoch_ms", *payload.Find("epoch_ms")},
                  {"expires_at", *payload.Find("expires_at")},
                  {"parallel", Boolean(payload.GetBool("parallel", true))},
                  {"calls", Array()},
                  {"results", Array()}});
      states.push_back(std::move(state));
      continue;
    }
    const auto* caller = item.Find("caller");
    const bool program_call = caller && caller->GetString("type") == "program";
    if (type == "program_output") {
      const auto found = owners.find(item.GetString("call_id"));
      auto shape = Shape(item, path);
      if (!shape.ok()) return shape;
      if (found == owners.end() || !states[found->second].terminal.IsNull())
        return Invalid("Orphan or duplicate program output", path);
      auto& state = states[found->second];
      if (item.GetString("status") == "completed" &&
          state.worker_input.GetArray("results")->array.size() !=
              state.calls.size())
        return Invalid("Program output precedes outstanding tool results",
                       path);
      state.terminal = item;
      continue;
    }
    const bool invocation =
        type == "function_call" || type == "custom_tool_call";
    const bool output =
        type == "function_call_output" || type == "custom_tool_call_output";
    if (!invocation && !output) {
      if (program_call) return Invalid("Invalid program caller item", path);
      continue;
    }
    const auto known = calls.find(item.GetString("call_id"));
    if (!program_call) {
      if (known != calls.end())
        return Invalid("Program child requires its original caller", path);
      continue;
    }
    const auto owner = owners.find(caller->GetString("caller_id"));
    if (owner == owners.end()) return Invalid("Orphan program caller", path);
    auto& state = states[owner->second];
    if (!Same(*caller, Caller(state.program)) || !state.terminal.IsNull())
      return Invalid("Program caller changed or program already ended", path);
    auto shape = Shape(item, path);
    if (!shape.ok()) return shape;
    if (invocation) {
      const auto index = state.calls.size();
      const auto key = ResponsesToolModelName(
          item.GetString("namespace"), item.GetString("name"),
          type == "function_call" ? "function" : "custom");
      const auto* tool = FindTool(state.tools, key);
      if (!tool || index >= kCalls ||
          item.GetString("status", "completed") != "completed")
        return Invalid("Unauthorized or incomplete program child", path);
      auto call = Object(
          {{"key", String(key)},
           {"arguments", String(item.GetString(
                             type == "function_call" ? "arguments" : "input"))},
           {"after_results",
            Number(state.worker_input.GetArray("results")->array.size())}});
      auto canonical = item;
      if (!canonical.Has("status"))
        Set(&canonical, "status", String("completed"));
      std::erase_if(canonical.object, [](const auto& field) {
        return field.first == "created_by" ||
               (field.first == "namespace" && field.second.IsNull()) ||
               (field.first == "async" && field.second.IsBool() &&
                !field.second.boolean);
      });
      if (!Same(Child(state.program, *tool, call, index), canonical) ||
          !calls
               .emplace(item.GetString("call_id"),
                        std::make_pair(owner->second, index))
               .second)
        return Invalid("Program child identity was changed", path);
      state.calls.push_back(item);
      Field(state.worker_input, "calls").array.push_back(std::move(call));
    } else {
      if (known == calls.end() || known->second.first != owner->second ||
          !returned.insert(item.GetString("call_id")).second)
        return Invalid("Orphan, duplicate or cross-program result", path);
      const auto index = known->second.second;
      const auto& call = state.calls[index];
      const bool custom = call.GetString("type") == "custom_tool_call";
      if (custom != (type == "custom_tool_call_output") ||
          item.GetString("status", "completed") != "completed")
        return Invalid("Program result type or status changed", path);
      for (const auto* field : {"name", "namespace"})
        if (Nonnull(item, field) &&
            item.GetString(field) != call.GetString(field))
          return Invalid("Program result identity changed", path);
      const auto* output_value = item.Find("output");
      if (!output_value) return Invalid("Missing program result", path);
      const auto* tool = FindTool(
          state.tools,
          state.worker_input.GetArray("calls")->array[index].GetString("key"));
      const auto* schema = tool->Find("definition")->Find("output_schema");
      if (schema && !schema->IsNull() && output_value->IsString())
        checks.array.push_back(Object({{"schema", *schema},
                                       {"output", *output_value},
                                       {"param", String(path + ".output")}}));
      auto value = *output_value;
      if (!custom && output_value->IsString()) {
        Json decoded;
        if (io::ParseJson(output_value->str, &decoded, true, kSchemaJsonLimits))
          value = std::move(decoded);
      }
      Field(state.worker_input, "results")
          .array.push_back(
              Object({{"index", Number(index)}, {"value", std::move(value)}}));
    }
  }
  auto validated = ValidateToolResultSchemas(
      checks, {access.scope, access.deadline, access.cancelled});
  if (!validated.ok()) return validated;
  *programs = std::move(states);
  return {};
}

ObjectReply AdvanceResponsesProgram(const ResponsesProgramState& program,
                                    const StateAccess& access, Json* emitted) {
  if (!emitted) return Invalid("Missing program output owner");
  *emitted = Array();
  auto active = Active(access);
  if (!active.ok()) return active;
  if (!program.terminal.IsNull()) return {};
  if (program.worker_input.GetInt("expires_at") <= std::time(nullptr))
    return Invalid("Program continuation has expired", "input.fingerprint");
  auto executed = ExecuteProgramWorker(
      program.worker_input, {access.scope, access.deadline, access.cancelled});
  if (!executed.ok()) return executed;
  const auto& calls = *executed.value.GetArray("calls");
  if (executed.value.GetString("error") == "program_replay_mismatch" ||
      (executed.value.GetString("status") != "incomplete" &&
       calls.array.size() < program.calls.size()))
    return Invalid("Program history does not match its execution");
  auto validated = CallsContract(program, calls, access);
  if (!validated.ok()) {
    if (validated.http_status == 400)
      return Error(502, "program_arguments_invalid", validated.message);
    return validated;
  }
  auto result = Array();
  for (size_t i = program.calls.size(); i < calls.array.size(); ++i) {
    const auto* tool = FindTool(program.tools, calls.array[i].GetString("key"));
    result.array.push_back(Child(program.program, *tool, calls.array[i], i));
  }
  if (executed.value.GetString("status") != "suspended") {
    std::string output;
    if (executed.value.GetString("status") == "incomplete") {
      output = OutputJson(Object({{"error", *executed.value.Find("error")}}));
    } else {
      auto content = Array();
      for (const auto& value : executed.value.GetArray("output")->array) {
        const bool image = value.GetString("type") == "image";
        content.array.push_back(
            Object({{"type", String(image ? "input_image" : "input_text")},
                    {image ? "image_url" : "text", *value.Find("value")}}));
      }
      auto encoded = EncodeResponsesProgramOutput(content, &output);
      if (!encoded)
        return Error(502, "program_output_invalid", encoded.message());
    }
    result.array.push_back(Object(
        {{"id",
          String("prog_out_" +
                 Digest(program.program.GetString("call_id")).substr(0, 56))},
         {"call_id", *program.program.Find("call_id")},
         {"type", String("program_output")},
         {"status", String(executed.value.GetString("status"))},
         {"result", String(std::move(output))}}));
  }
  *emitted = std::move(result);
  return {};
}

ObjectReply BindResponsesProgramContracts(const ResponsesProgramState& program,
                                          const Json& items, Json* contracts) {
  if (!items.IsArray()) return Invalid("Invalid program child contracts");
  for (const auto& item : items.array) {
    if (!IsResultBoundToolCall(item)) continue;
    if (!item.Find("caller") ||
        !Same(*item.Find("caller"), Caller(program.program)))
      continue;
    const auto key = ResponsesToolModelName(
        item.GetString("namespace"), item.GetString("name"),
        item.GetString("type") == "function_call" ? "function" : "custom");
    const auto* tool = FindTool(program.tools, key);
    if (!tool) return Invalid("Program schema outside sealed authority");
    auto definition = *tool->Find("definition");
    // The snapshot is already loaded at invocation, even when its original
    // declaration carried defer_loading. Do not erase the sealed original.
    Set(&definition, "defer_loading", Boolean(false));
    if (!tool->GetString("namespace").empty())
      definition = Object({{"type", String("namespace")},
                           {"name", *tool->Find("namespace")},
                           {"tools", Array({std::move(definition)})}});
    auto result = BindToolResultContracts(
        Object({{"tools", Array({std::move(definition)})}}), Array({item}),
        contracts, true);
    if (!result.ok()) return result;
  }
  return {};
}
}  // namespace q4t::server
