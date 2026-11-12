#include "q4t/server/responses_output.h"

#include <limits>

#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_item_shape.h"
#include "responses_json.h"

namespace q4t::server {
using namespace responses_detail;

Status ResponsesOutput::BindTools(const io::Json& active) {
  if (terminal_) return Status::Fail("Cannot bind tools after completion");
  std::vector<ResponsesToolIdentity> identities;
  ChatPlanError error;
  auto status = PlanResponsesToolIdentities(Object({{"tools", active}}),
                                            &identities, &error);
  if (status) tool_identities_ = std::move(identities);
  return status;
}

Status ResponsesOutput::StartToolSearch(const ResponseToolCall& call,
                                        bool server, size_t* index,
                                        std::string* wire) {
  *index = kMaxItems;
  wire->clear();
  if (!started_ || terminal_ || call.id.empty() || call.custom ||
      response_.GetString("status") != "in_progress" ||
      Items().array.size() >= kMaxItems)
    return Status::Fail("Invalid tool search start");
  auto item = Object({{"id", String(detail::NewRequestId("ts_"))},
                      {"type", String("tool_search_call")},
                      {"status", String("in_progress")},
                      {"execution", String(server ? "server" : "client")},
                      {"call_id", server ? io::Json() : String(call.id)},
                      {"arguments", Object()}});
  auto status = Charge(OutputJson(item).size() + 8 * sizeof(io::Json));
  if (!status) return status;
  *index = Items().array.size();
  Items().array.push_back(std::move(item));
  call_indices_.push_back(call.index);
  Event("response.output_item.added",
        Object({{"output_index", Number(*index)},
                {"item", Items().array[*index]}}),
        wire);
  return {};
}

Status ResponsesOutput::CompleteToolSearchCall(size_t index,
                                               const io::Json& arguments,
                                               bool incomplete,
                                               std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      Items().array[index].GetString("type") != "tool_search_call" ||
      closed_items_.contains(index))
    return Status::Fail("Invalid tool search completion");
  auto item = Items().array[index];
  Set(&item, "arguments", arguments);
  Set(&item, "status", String(incomplete ? "incomplete" : "completed"));
  ChatPlanError error;
  auto status = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                           &error, "tool_search_call");
  if (status) status = Charge(OutputJson(arguments).size());
  if (!status) return status;
  Items().array[index] = std::move(item);
  CloseItem(index, incomplete, wire);
  return {};
}

Status ResponsesOutput::CompleteToolSearchOutput(size_t call_index,
                                                 const io::Json& tools,
                                                 std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || call_index >= Items().array.size() ||
      Items().array[call_index].GetString("type") != "tool_search_call" ||
      Items().array[call_index].GetString("execution") != "server" ||
      Items().array[call_index].GetString("status") != "completed" ||
      !closed_items_.contains(call_index) ||
      tool_search_results_.contains(call_index) || !tools.IsArray() ||
      Items().array.size() >= kMaxItems)
    return Status::Fail("Invalid tool search result");
  auto item = Object({{"id", String(detail::NewRequestId("tso_"))},
                      {"type", String("tool_search_output")},
                      {"execution", String("server")},
                      {"status", String("completed")},
                      {"call_id", io::Json()},
                      {"tools", tools}});
  ChatPlanError error;
  auto status = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                           &error, "tool_search_output");
  if (status) status = Charge(OutputJson(item).size() + 8 * sizeof(io::Json));
  if (!status) return status;
  const auto index = Items().array.size();
  Items().array.push_back(std::move(item));
  tool_search_results_.insert(call_index);
  call_indices_.push_back(std::numeric_limits<size_t>::max());
  Event(
      "response.output_item.added",
      Object({{"output_index", Number(index)}, {"item", Items().array[index]}}),
      wire);
  CloseItem(index, false, wire);
  return {};
}
}  // namespace q4t::server
