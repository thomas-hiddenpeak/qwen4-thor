#include "q4t/server/batch_response_link.h"

#include <ctime>

#include "object_protocol.h"
#include "q4t/server/batch_state.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/responses_state.h"

namespace q4t::server {
using namespace object_detail;
Json BatchResponseLinkValue(const BatchResponseLink& link) {
  return Object({{"format", String("q4t.batch_response.v1")},
                 {"batch_id", String(link.batch_id)},
                 {"response_id", String(link.response_id)},
                 {"request_id", String(link.request_id)},
                 {"scope", String(link.scope)},
                 {"index", Number(link.index)}});
}
bool ParseBatchResponseLink(const Json& value, BatchResponseLink* link) {
  const auto* index = value.Find("index");
  BatchResponseLink parsed{
      value.GetString("batch_id"), value.GetString("response_id"),
      value.GetString("request_id"), value.GetString("scope"), 0};
  if (!link || !value.IsObject() || value.object.size() != 6 ||
      value.GetString("format") != "q4t.batch_response.v1" ||
      !parsed.batch_id.starts_with("batch_") ||
      !parsed.response_id.starts_with("resp_") ||
      !parsed.request_id.starts_with("req_tool_run_") ||
      !ValidApplicationId(parsed.batch_id) ||
      !ValidApplicationId(parsed.response_id) ||
      !ValidApplicationId(parsed.request_id) || parsed.scope.empty() ||
      parsed.scope.size() > 256 || !index || !IsInputInteger(*index) ||
      index->AsDouble() < 0 || index->AsDouble() >= kBatchRequests)
    return false;
  parsed.index = static_cast<size_t>(index->AsInt(-1));
  *link = std::move(parsed);
  return true;
}
ObjectReply CheckBatchResponseParent(ObjectStore& objects,
                                     const BatchResponseLink& link,
                                     const StateAccess& access,
                                     bool* cancelled) {
  BatchResponseLink checked;
  if (!cancelled || access.scope != link.scope ||
      !ParseBatchResponseLink(BatchResponseLinkValue(link), &checked))
    return Error(503, "batch_response_binding_invalid",
                 "Invalid trusted Batch response binding");
  *cancelled = false;
  BatchState batches(objects);
  const BinaryAccess binary{access.scope, access.deadline, access.cancelled};
  auto loaded = batches.Load(binary, link.batch_id);
  if (!loaded.ok()) return loaded;
  const auto& data = *loaded.value.Find("data");
  const auto& batch = *data.Find("batch");
  if (BatchTerminal(batch.GetString("status")) ||
      data.GetInt("next_row") != static_cast<int64_t>(link.index))
    return Error(409, "batch_response_parent_completed",
                 "The original Batch row no longer owns this execution");
  auto row = batches.Row(binary, link.batch_id, link.index);
  if (!row.ok()) return row;
  const auto* saved = row.value.Find("background_response");
  if (row.value.GetString("status") != "running" || !saved ||
      !ParseBatchResponseLink(*saved, &checked) || checked != link ||
      row.value.GetString("request_id") != link.request_id)
    return Error(503, "batch_response_binding_invalid",
                 "Original Batch row and response identities disagree");
  *cancelled = batch.GetString("status") == "cancelling" ||
               batch.GetString("status") == "finalizing" ||
               batch.GetInt("expires_at") <= std::time(nullptr);
  return {};
}
}  // namespace q4t::server
