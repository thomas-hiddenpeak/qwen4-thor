#include "q4t/server/responses_output.h"

#include <limits>

#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_file_search.h"
#include "q4t/server/responses_item_shape.h"
#include "responses_json.h"

namespace q4t::server {
using namespace responses_detail;

Status ResponsesOutput::StartFileSearch(size_t* index, std::string* wire) {
  wire->clear();
  *index = kMaxItems;
  if (!started_ || terminal_ ||
      response_.GetString("status") != "in_progress" ||
      Items().array.size() >= kMaxItems)
    return Status::Fail("File search cannot start outside active response");
  auto item = Object({{"id", String(detail::NewRequestId("fs_"))},
                      {"type", String("file_search_call")},
                      {"status", String("in_progress")},
                      {"queries", Array()},
                      {"results", io::Json()}});
  auto status = Charge(OutputJson(item).size() + 8 * sizeof(io::Json));
  if (!status) return status;
  *index = Items().array.size();
  Items().array.push_back(std::move(item));
  call_indices_.push_back(std::numeric_limits<size_t>::max());
  Event("response.output_item.added",
        Object({{"output_index", Number(*index)},
                {"item", Items().array[*index]}}),
        wire);
  Event("response.file_search_call.in_progress", Location(*index), wire);
  return {};
}

Status ResponsesOutput::SearchFileSearch(size_t index, const io::Json& queries,
                                         std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      Items().array[index].GetString("type") != "file_search_call" ||
      Items().array[index].GetString("status") != "in_progress" ||
      !queries.IsArray())
    return Status::Fail("Invalid file search execution transition");
  VectorSearchPlan checked;
  ChatPlanError error;
  auto status =
      PlanVectorSearch(Object({{"query", queries}}), &checked, &error);
  if (!status) return status;
  status = Charge(OutputJson(queries).size());
  if (!status) return status;
  Set(&Items().array[index], "queries", queries);
  Set(&Items().array[index], "status", String("searching"));
  Event("response.file_search_call.searching", Location(index), wire);
  return {};
}

Status ResponsesOutput::CompleteFileSearch(size_t index,
                                           const io::Json& results,
                                           std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      Items().array[index].GetString("type") != "file_search_call" ||
      Items().array[index].GetString("status") != "searching" ||
      !results.IsArray() || results.array.size() > 50)
    return Status::Fail("Invalid file search completion transition");
  auto item = Items().array[index];
  Set(&item, "results", results);
  Set(&item, "status", String("completed"));
  ChatPlanError error;
  auto status = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                           &error, "file_search_call");
  if (!status) return status;
  status = Charge(OutputJson(results).size() +
                  results.array.size() * 8 * sizeof(io::Json));
  if (!status) return status;
  Items().array[index] = std::move(item);
  Event("response.file_search_call.completed", Location(index), wire);
  CloseItem(index, false, wire);
  return {};
}

Status ResponsesOutput::FailFileSearch(size_t index, std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      Items().array[index].GetString("type") != "file_search_call" ||
      closed_items_.contains(index))
    return Status::Fail("Invalid file search failure transition");
  Set(&Items().array[index], "status", String("failed"));
  Event(
      "response.output_item.done",
      Object({{"output_index", Number(index)}, {"item", Items().array[index]}}),
      wire);
  closed_items_.insert(index);
  return {};
}
Status ResponsesOutput::SkipFileSearch(size_t index, const io::Json& queries,
                                       std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      Items().array[index].GetString("type") != "file_search_call" ||
      closed_items_.contains(index))
    return Status::Fail("Invalid file search skip transition");
  VectorSearchPlan checked;
  ChatPlanError error;
  auto status =
      PlanVectorSearch(Object({{"query", queries}}), &checked, &error);
  if (status) status = Charge(OutputJson(queries).size());
  if (!status) return status;
  Set(&Items().array[index], "queries", queries);
  CloseItem(index, true, wire);
  return {};
}
}  // namespace q4t::server
