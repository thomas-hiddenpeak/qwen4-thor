#include "q4t/server/batch_finalization.h"

#include "object_protocol.h"
#include "q4t/server/batch_input.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool NumberIn(const Json& value, const std::string& key, int64_t maximum,
              int64_t* result) {
  const auto* field = value.Find(key);
  if (!field || !IsInputInteger(*field) || field->number < 0 ||
      field->number > static_cast<double>(maximum))
    return false;
  *result = field->AsInt(-1);
  return *result >= 0 && *result <= maximum;
}
}  // namespace

Json BatchOutputFailureJson(const BatchOutputFailure& failure) {
  return Object({{"format", String("q4t.batch_output_failure.v1")},
                 {"row_stop", Number(failure.row_stop)},
                 {"reason", String(failure.reason)},
                 {"retain_until", Number(failure.retain_until)}});
}
bool ParseBatchOutputFailure(const Json& value, BatchOutputFailure* out) {
  int64_t row = 0, until = 0;
  const auto reason = value.GetString("reason");
  if (!out || !value.IsObject() || value.object.size() != 4 ||
      value.GetString("format") != "q4t.batch_output_failure.v1" ||
      !NumberIn(value, "row_stop", kBatchRequests - 1, &row) ||
      !NumberIn(value, "retain_until", (int64_t{1} << 53) - 1, &until) ||
      (reason != "bytes" && reason != "chunks"))
    return false;
  *out = {static_cast<size_t>(row), reason, until};
  return true;
}
bool ValidBatchOutputFailure(const Json& data) {
  const auto* value = data.Find("output_failure");
  if (!value) return true;
  BatchOutputFailure failure;
  const auto* batch = data.Find("batch");
  const auto* counts = batch ? batch->Find("request_counts") : nullptr;
  int64_t total = 0;
  if (!ParseBatchOutputFailure(*value, &failure) || !batch || !counts ||
      !NumberIn(*counts, "total", kBatchRequests, &total) ||
      failure.row_stop >= static_cast<uint64_t>(total) ||
      data.GetInt("next_row", -1) != total || data.GetInt("running", -1) != 0)
    return false;
  const auto status = batch->GetString("status");
  if (status == "finalizing")
    return failure.retain_until == 0 && !data.GetBool("resources_released");
  int64_t failed_at = 0, ttl = 0;
  if (status != "failed" || !data.GetBool("resources_released") ||
      !NumberIn(*batch, "failed_at", (int64_t{1} << 53) - 1, &failed_at) ||
      failed_at == 0 ||
      !NumberIn(data, "output_ttl_seconds", 30 * 24 * 3600, &ttl) ||
      ttl < 3600 || failure.retain_until != failed_at + ttl)
    return false;
  const auto* errors = batch->Find("errors");
  const auto* list = errors ? errors->GetArray("data") : nullptr;
  return errors && errors->GetString("object") == "list" && list &&
         list->array.size() == 1 &&
         list->array[0].GetString("code") == "batch_output_capacity_exceeded" &&
         list->array[0].GetInt("line") ==
             static_cast<int64_t>(failure.row_stop + 1);
}
bool ValidBatchOutputFailureTransition(const Json& data, const Json* previous) {
  if (!ValidBatchOutputFailure(data)) return false;
  const auto* next = data.Find("output_failure");
  const auto* old = previous ? previous->Find("output_failure") : nullptr;
  if (!next) return !old;
  const auto* old_batch = previous ? previous->Find("batch") : nullptr;
  if (!old_batch) return false;
  if (!old)
    return old_batch->GetString("status") == "finalizing" &&
           data.Find("batch")->GetString("status") == "finalizing";
  BatchOutputFailure a, b;
  if (!ParseBatchOutputFailure(*old, &a) ||
      !ParseBatchOutputFailure(*next, &b) || a.reason != b.reason)
    return false;
  if (a.row_stop != b.row_stop)
    return b.row_stop < a.row_stop && a.retain_until == 0 &&
           b.retain_until == 0 &&
           old_batch->GetString("status") == "finalizing" &&
           data.Find("batch")->GetString("status") == "finalizing";
  return a.retain_until == b.retain_until ||
         (a.retain_until == 0 && b.retain_until > 0 &&
          old_batch->GetString("status") == "finalizing" &&
          data.Find("batch")->GetString("status") == "failed");
}
}  // namespace q4t::server
