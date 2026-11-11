#include "q4t/server/batch_usage.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Count(const Json& value, const std::string& key, uint64_t* out,
           uint64_t maximum = kBatchTokenLimit) {
  const auto* number = value.Find(key);
  if (!number || !IsInputInteger(*number) || number->number < 0 ||
      number->number > static_cast<double>(maximum) || number->AsInt(-1) < 0 ||
      static_cast<uint64_t>(number->AsInt()) > maximum)
    return false;
  *out = number->AsInt();
  return true;
}
bool ParseUsage(const Json& value, BatchUsage* out, bool chat = false,
                bool opaque = false) {
  uint64_t total = 0;
  if (!value.IsObject() ||
      !Count(value, chat ? "prompt_tokens" : "input_tokens", &out->input) ||
      !Count(value, chat ? "completion_tokens" : "output_tokens",
             &out->output) ||
      !Count(value, "total_tokens", &total) ||
      out->output > kBatchTokenLimit - out->input ||
      total != out->input + out->output)
    return false;
  const auto* input =
      value.Find(chat ? "prompt_tokens_details" : "input_tokens_details");
  const auto* output =
      value.Find(chat ? "completion_tokens_details" : "output_tokens_details");
  if (opaque && !input && !output) return true;
  return input && input->IsObject() && output && output->IsObject() &&
         Count(*input, "cached_tokens", &out->cached, out->input) &&
         Count(*output, "reasoning_tokens", &out->reasoning, out->output);
}
void Publish(Json* data, const Json& accounting) {
  auto& batch = Field(*data, "batch");
  std::erase_if(batch.object,
                [](const auto& field) { return field.first == "usage"; });
  if (accounting.GetInt("rows") == data->GetInt("next_row") &&
      accounting.GetInt("rows") > 0 && !accounting.GetInt("unknown_rows"))
    Set(&batch, "usage", *accounting.Find("totals"));
}
}  // namespace

Json BatchUsageJson(const BatchUsage& usage) {
  return Object({{"input_tokens", Number(usage.input)},
                 {"output_tokens", Number(usage.output)},
                 {"total_tokens", Number(usage.input + usage.output)},
                 {"input_tokens_details",
                  Object({{"cached_tokens", Number(usage.cached)}})},
                 {"output_tokens_details",
                  Object({{"reasoning_tokens", Number(usage.reasoning)}})}});
}
std::optional<BatchUsage> BatchResponseUsage(std::string_view endpoint,
                                             const Json& body) {
  const auto* usage = body.Find("usage");
  BatchUsage result;
  if (!usage ||
      (endpoint != "/v1/responses" && endpoint != "/v1/chat/completions" &&
       endpoint != "/v1/completions") ||
      !ParseUsage(*usage, &result, endpoint != "/v1/responses",
                  endpoint == "/v1/completions"))
    return std::nullopt;
  return result;
}
size_t BatchUsageRows(const Json& data) {
  const auto* accounting = data.Find("usage_accounting");
  return accounting ? accounting->GetInt("rows") : 0;
}
std::optional<BatchUsage> BatchCapturedUsage(const Json& row) {
  const auto* value = row.Find("actual_usage");
  BatchUsage usage;
  if (!value || !ParseUsage(*value, &usage)) return {};
  return usage;
}
bool ValidBatchCapturedUsage(const Json& row) {
  if (!row.Has("actual_usage")) return true;
  const auto status = row.GetString("status");
  return (status == "completed" || status == "failed") &&
         !row.GetString("request_id").empty() &&
         BatchCapturedUsage(row).has_value();
}
bool ValidBatchUsageAccounting(const Json& data) {
  const auto* accounting = data.Find("usage_accounting");
  const auto* batch = data.Find("batch");
  if (!batch) return false;
  if (!accounting) return !batch->Has("usage");
  uint64_t rows = 0, unknown = 0;
  BatchUsage totals;
  const auto* usage = accounting->Find("totals");
  if (!accounting->IsObject() || accounting->object.size() != 4 ||
      accounting->GetString("format") != "q4t.batch_usage.v1" ||
      data.GetInt("next_row") < 0 ||
      !Count(*accounting, "rows", &rows, data.GetInt("next_row")) ||
      !Count(*accounting, "unknown_rows", &unknown, rows) || !usage ||
      !ParseUsage(*usage, &totals))
    return false;
  const auto* published = batch->Find("usage");
  const bool complete =
      rows == static_cast<uint64_t>(data.GetInt("next_row")) && rows > 0 &&
      unknown == 0;
  return complete ? published && OutputJson(*published) == OutputJson(*usage)
                  : !published;
}
ObjectReply AddBatchUsage(Json* data, size_t index,
                          const std::optional<BatchUsage>& usage) {
  // next_row has advanced locally for a new FinishRow; the old published
  // prefix still legitimately describes the preceding checkpoint.
  const auto* previous = data->Find("usage_accounting");
  if (BatchUsageRows(*data) != index)
    return Error(503, "batch_usage_recount_required",
                 "Original results require usage reconciliation");
  BatchUsage totals;
  uint64_t unknown = 0;
  if (previous) {
    const auto* value = previous->Find("totals");
    if (!value || !ParseUsage(*value, &totals) ||
        !Count(*previous, "unknown_rows", &unknown, index))
      return Error(503, "batch_state_invalid", "Invalid Batch usage");
  }
  if (!usage || usage->input > kBatchTokenLimit - totals.input ||
      usage->output > kBatchTokenLimit - totals.output ||
      usage->cached > usage->input || usage->reasoning > usage->output ||
      usage->output > kBatchTokenLimit - usage->input ||
      totals.input + totals.output >
          kBatchTokenLimit - usage->input - usage->output) {
    ++unknown;  // Never clamp/overflow an actual count into a false total.
  } else {
    totals.input += usage->input;
    totals.output += usage->output;
    totals.cached += usage->cached;
    totals.reasoning += usage->reasoning;
  }
  auto accounting = Object({{"format", String("q4t.batch_usage.v1")},
                            {"rows", Number(index + 1)},
                            {"unknown_rows", Number(unknown)},
                            {"totals", BatchUsageJson(totals)}});
  Publish(data, accounting);
  Set(data, "usage_accounting", std::move(accounting));
  return {};
}
}  // namespace q4t::server
