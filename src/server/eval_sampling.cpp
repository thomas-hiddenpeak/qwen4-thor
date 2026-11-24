#include "eval_sampling.h"

#include <algorithm>
#include <charconv>
#include <ctime>

#include "eval_grading.h"
#include "eval_run_state.h"
#include "q4t/server/request_json.h"

namespace q4t::server::eval_detail {
namespace {
using namespace object_detail;
ObjectReply Render(Json* node, const Json& row, size_t* budget) {
  if (node->IsString()) {
    std::string value;
    auto result =
        RenderText(node->str, row, "input_messages.template", budget, &value);
    if (result.ok()) node->str = std::move(value);
    return result;
  }
  for (auto& child : node->array) {
    auto result = Render(&child, row, budget);
    if (!result.ok()) return result;
  }
  for (auto& [key, child] : node->object) {
    auto result = Render(&child, row, budget);
    if (!result.ok()) return result;
  }
  return {};
}

Json Copy(const Json& data, const char* key, Json otherwise = {}) {
  const auto* value = data.Find(key);
  return value ? *value : std::move(otherwise);
}
ObjectReply Pending() {
  ObjectReply result;
  result.http_status = 202;
  return result;
}

ObjectReply CompletionTools(const Json& tools, Json* normalized) {
  *normalized = Array();
  for (const auto& tool : tools.array) {
    const auto type = tool.GetString("type");
    if (type == "function" || type == "custom") {
      const auto* inner = tool.Find(type);
      if (!inner || !inner->IsObject())
        return BadInput("tools", "Invalid Chat tool");
      auto value = *inner;
      Set(&value, "type", String(type));
      normalized->array.push_back(std::move(value));
    } else
      normalized->array.push_back(tool);
  }
  return {};
}

Json MessageUsage(const Json& response) {
  const auto usage = Copy(response, "usage", Object());
  const auto details = Copy(usage, "input_tokens_details");
  return Object({{"prompt_tokens", Number(usage.GetInt("input_tokens"))},
                 {"completion_tokens", Number(usage.GetInt("output_tokens"))},
                 {"total_tokens", Number(usage.GetInt("total_tokens"))},
                 {"cached_tokens", Number(details.GetInt("cached_tokens"))}});
}
Json Messages(const Json& input) {
  auto list = Array();
  if (input.IsString()) {
    list.array.push_back(
        Object({{"role", String("user")}, {"content", input}}));
    return list;
  }
  for (const auto& item : input.array) {
    const auto* content = item.Find("content");
    if (!content || !item.Has("role")) continue;
    std::string text;
    if (content->IsString())
      text = content->str;
    else
      for (const auto& part : content->array)
        if (part.GetString("type") == "input_text" ||
            part.GetString("type") == "output_text")
          text += part.GetString("text");
    list.array.push_back(Object(
        {{"role", Copy(item, "role")}, {"content", String(std::move(text))}}));
  }
  return list;
}

ObjectReply Projection(const Json& response, const Json& request,
                       Json* sample) {
  auto output = Array(), tools = Array();
  std::string text, reasoning;
  const auto* items = response.GetArray("output");
  if (!items) return InvalidRecord();
  for (const auto& item : items->array) {
    const auto type = item.GetString("type");
    if (type == "message") {
      auto messages = Messages(Array({item}));
      for (auto& entry : messages.array) {
        text += entry.GetString("content");
        output.array.push_back(std::move(entry));
      }
    } else if (type == "function_call" || type == "custom_tool_call") {
      tools.array.push_back(item);
    } else if (type == "reasoning") {
      if (const auto* summary = item.GetArray("summary"))
        for (const auto& part : summary->array)
          reasoning += part.GetString("text");
    }
  }
  const auto finish = response.GetString("status") == "incomplete"
                          ? "length"
                          : (!tools.array.empty() ? "tool_calls" : "stop");
  *sample = Object(
      {{"model", Copy(response, "model")},
       {"input", Messages(Copy(request, "input"))},
       {"output", std::move(output)},
       {"output_text", String(text)},
       {"output_tools", std::move(tools)},
       {"input_tools", Copy(request, "tools", Array())},
       {"output_reasoning_summary", String(std::move(reasoning))},
       {"usage", MessageUsage(response)},
       {"finish_reason", String(finish)},
       {"temperature", Copy(response, "temperature", Number(1))},
       {"top_p", Copy(response, "top_p", Number(1))},
       {"max_completion_tokens", Copy(request, "max_output_tokens", Number(0))},
       {"seed", Copy(request, "seed", Number(0))},
       {"error", Json()}});
  Json object;
  if (io::ParseJson(text, &object, true, kApplicationJsonLimits) &&
      object.IsObject())
    Set(sample, "output_json", std::move(object));
  auto choice =
      Object({{"role", String("assistant")}, {"content", String(text)}});
  Set(sample, "choices",
      Array({Object({{"index", Number(0)},
                     {"message", std::move(choice)},
                     {"finish_reason", String(finish)}})}));
  return {};
}
}  // namespace

ObjectReply SamplingRequest(const Json& source, const Json& row,
                            const ChatPlanConfig& config, Json* request) {
  const auto* messages = source.Find("input_messages");
  Json input;
  if (messages && messages->GetString("type") == "template") {
    input = Copy(*messages, "template");
    size_t budget = kChatBodyBytes;
    auto rendered = Render(&input, row, &budget);
    if (!rendered.ok()) return rendered;
  } else {
    const auto reference = messages ? messages->GetString("item_reference")
                                    : "item.input_trajectory";
    const auto* resolved = ResolveVariable(reference, row);
    if (!resolved && !messages) resolved = ResolveVariable("item.input", row);
    if (!resolved)
      return BadInput("input_messages",
                      "Dataset does not contain the input trajectory");
    input = *resolved;
  }
  if (!input.IsArray() && !input.IsString())
    return BadInput("input_messages",
                    "Expected an input message array or text");
  // EvalItem permits a single content part where Responses accepts a list.
  for (auto& message : input.array) {
    const auto* part = message.Find("content");
    if (part && part->IsObject()) Set(&message, "content", Array({*part}));
    if (auto* array = message.GetArray("content"); array) {
      for (auto& value : Field(message, "content").array)
        if (value.IsString())
          value = Object({{"type", String("input_text")}, {"text", value}});
    }
  }
  *request = Object({{"model", String(source.GetString("model", config.model))},
                     {"input", std::move(input)},
                     {"background", Boolean(true)},
                     {"store", Boolean(true)}});
  const auto* options = source.Find("sampling_params");
  if (!options || options->IsNull()) return {};
  for (const auto& [name, value] : options->object) {
    if (value.IsNull()) continue;
    if (name == "reasoning_effort")
      Set(request, "reasoning", Object({{"effort", value}}));
    else if (name == "max_completion_tokens")
      Set(request, "max_output_tokens", value);
    else if (name == "response_format") {
      Json format = value;
      if (value.GetString("type") == "json_schema") {
        format = Copy(value, "json_schema");
        Set(&format, "type", String("json_schema"));
      }
      Set(request, "text", Object({{"format", std::move(format)}}));
    } else if (name == "tools" && source.GetString("type") == "completions") {
      Json tools;
      auto checked = CompletionTools(value, &tools);
      if (!checked.ok()) return checked;
      Set(request, "tools", std::move(tools));
    } else
      Set(request, name, value);
  }
  return {};
}

ObjectReply SampleRow(ObjectStore& objects, const StateAccess& access,
                      SamplingOwner& owner, ObjectRequest& write,
                      const Json& source, Json* row, ObjectRequest* consumed) {
  const auto id = write.data.GetString("pending_response");
  if (id.empty()) {
    Json request;
    auto translated = SamplingRequest(source, *row, owner.config, &request);
    if (!translated.ok()) return translated;
    if (!request.Has("seed")) {
      std::string identity =
          write.id + ":" + std::to_string(write.data.GetInt("row"));
      if (const auto* grades = write.data.Find("model_results"))
        identity += ":grader:" + std::to_string(grades->object.size());
      const auto digest = Digest(identity);
      int64_t seed = 0;
      // Thirteen hexadecimal digits are exact even for JSON double clients.
      std::from_chars(digest.data(), digest.data() + 13, seed, 16);
      Set(&request, "seed", Number(seed));
    }
    auto config = owner.config;
    config.client_limits = owner.client.Limits();
    ClientLease permit;
    if (!permit.Acquire(owner.clients, owner.client, ClientLane::kData))
      return Error(429, "client_capacity_exhausted",
                   "Evaluation client is at capacity");
    auto child =
        std::make_unique<ResponsesExecution>(objects, &owner.jobs, true);
    auto prepared =
        child->Prepare(request, config, access, std::time(nullptr), true);
    if (!prepared.ok()) return prepared;
    const auto* budget = child->Envelope().options.Find("max_output_tokens");
    if (!budget || !budget->IsNumber()) return InvalidRecord();
    Set(&request, "max_output_tokens", *budget);
    auto claim = write;
    Set(&claim.data, "pending_response", String(child->Id()));
    Set(&claim.data, "pending_input", *row);
    Set(&claim.data, "sampling_request", std::move(request));
    auto bound = child->BindEvalSampling(std::move(claim));
    if (!bound.ok()) return bound;
    auto receipt = owner.jobs.Submit(
        std::move(child), owner.client,
        [&](ClientLease& dest) { return permit.TransferTo(dest); });
    return receipt.ok() ? Pending() : receipt;
  }
  auto observed = objects.Execute(Read(ObjectKind::kResponse, access, id),
                                  access.deadline, access.cancelled);
  if (!observed.ok()) return observed;
  const auto* data = observed.value.Find("data");
  const auto* response = data ? data->Find("response") : nullptr;
  if (!response || data->GetString("eval_run_id") != write.id ||
      response->GetString("id") != id)
    return InvalidRecord();
  const auto state = response->GetString("status");
  if (state == "in_progress" || state == "queued") return Pending();
  *consumed = Read(ObjectKind::kResponse, access, id);
  consumed->operation = ObjectOperation::kErase;
  consumed->expected_revision = observed.value.GetInt("revision");
  if (state != "completed" && state != "incomplete") {
    const auto error = Copy(*response, "error");
    return Error(422, error.GetString("code", "eval_sample_failed"),
                 error.GetString("message", "Evaluation model sample failed"));
  }
  const auto* input = write.data.Find("pending_input");
  const auto* request = write.data.Find("sampling_request");
  if (!input || !request) return InvalidRecord();
  *row = *input;
  Json sample;
  auto projected = Projection(*response, *request, &sample);
  if (!projected.ok()) return projected;
  Set(row, "sample", std::move(sample));
  return {};
}

void ClearSampling(Json& data) {
  std::erase_if(data.object, [](const auto& field) {
    return field.first == "pending_input" ||
           field.first == "sampling_request" ||
           field.first == "pending_response";
  });
}

ObjectReply ReleaseSampling(ObjectStore& objects, const StateAccess& access,
                            ResponsesJobs* jobs, ObjectRequest& parent) {
  const auto id = parent.data.GetString("pending_response");
  if (id.empty()) return {};
  auto erase = Read(ObjectKind::kResponse, access, id);
  for (;;) {
    auto current = objects.Execute(erase, access.deadline, access.cancelled);
    if (current.http_status == 404) break;
    if (!current.ok()) return current;
    const auto* data = current.value.Find("data");
    const auto* response = data ? data->Find("response") : nullptr;
    if (!response || data->GetString("eval_run_id") != parent.id ||
        response->GetString("id") != id)
      return InvalidRecord();
    const auto status = response->GetString("status");
    if (status != "queued" && status != "in_progress") {
      erase.operation = ObjectOperation::kErase;
      erase.expected_revision = current.value.GetInt("revision");
      break;
    }
    if (!jobs)
      return Error(503, "eval_worker_unavailable",
                   "Evaluation model owner unavailable");
    auto canceled = jobs->Cancel(id, access);
    if (!canceled.ok() && canceled.http_status != 404) return canceled;
  }
  ClearSampling(parent.data);
  std::vector<ObjectRequest> changes{parent};
  if (erase.operation == ObjectOperation::kErase)
    changes.push_back(std::move(erase));
  auto result =
      objects.ExecuteBatch(changes, access.deadline, access.cancelled);
  if (result.ok()) ++parent.expected_revision;
  return result;
}
}  // namespace q4t::server::eval_detail
