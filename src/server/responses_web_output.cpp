#include "q4t/server/responses_output.h"

#include <algorithm>
#include <limits>
#include <map>
#include <set>

#include "object_protocol.h"
#include "q4t/server/completion_response.h"
#include "q4t/server/http_response.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/responses_web_search.h"

namespace q4t::server {
using namespace object_detail;
Status ResponsesOutput::StartWebSearch(const io::Json& action, size_t* index,
                                       std::string* wire) {
  wire->clear();
  *index = kMaxItems;
  if (!started_ || terminal_ ||
      response_.GetString("status") != "in_progress" ||
      Items().array.size() >= kMaxItems)
    return Status::Fail("Web search requires an active response");
  auto item = Object({{"type", String("web_search_call")},
                      {"id", String(detail::NewRequestId("ws_"))},
                      {"status", String("in_progress")},
                      {"action", action}});
  ChatPlanError error;
  auto status = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                           &error, "web_search_call");
  if (status) status = Charge(OutputJson(item).size() + 8 * sizeof(io::Json));
  if (!status) return status;
  *index = Items().array.size();
  Items().array.push_back(std::move(item));
  call_indices_.push_back(std::numeric_limits<size_t>::max());
  Event("response.output_item.added",
        Object({{"output_index", Number(*index)},
                {"item", Items().array[*index]}}),
        wire);
  Event("response.web_search_call.in_progress", Location(*index), wire);
  return {};
}
Status ResponsesOutput::SearchWeb(size_t index, std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      Items().array[index].GetString("type") != "web_search_call" ||
      Items().array[index].GetString("status") != "in_progress")
    return Status::Fail("Invalid web search transition");
  Set(&Items().array[index], "status", String("searching"));
  Event("response.web_search_call.searching", Location(index), wire);
  return {};
}
Status ResponsesOutput::CompleteWebSearch(size_t index, const io::Json& action,
                                          std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      Items().array[index].GetString("type") != "web_search_call" ||
      Items().array[index].GetString("status") != "searching")
    return Status::Fail("Invalid web search completion");
  auto item = Items().array[index];
  Set(&item, "status", String("completed"));
  Set(&item, "action", action);
  ChatPlanError error;
  auto status = ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                           &error, "web_search_call");
  if (status) status = Charge(OutputJson(action).size() + 8 * sizeof(io::Json));
  if (!status) return status;
  Items().array[index] = std::move(item);
  Event("response.web_search_call.completed", Location(index), wire);
  CloseItem(index, false, wire);
  return {};
}
Status ResponsesOutput::EndWebSearch(size_t index, bool failed,
                                     std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || index >= Items().array.size() ||
      Items().array[index].GetString("type") != "web_search_call" ||
      closed_items_.contains(index))
    return Status::Fail("Invalid web search failure transition");
  Set(&Items().array[index], "status",
      String(failed ? "failed" : "incomplete"));
  Event(
      "response.output_item.done",
      Object({{"output_index", Number(index)}, {"item", Items().array[index]}}),
      wire);
  closed_items_.insert(index);
  return {};
}

Status ResponsesOutput::CiteWebSearchSources(const io::Json& contexts,
                                             std::string* wire) {
  wire->clear();
  if (!started_ || terminal_)
    return Status::Fail("Web citations require an active response");
  std::map<std::string, io::Json> sources;
  for (const auto& binding : contexts.array) {
    const auto* context = binding.Find("context");
    const auto* results = context ? context->GetArray("results") : nullptr;
    if (!results) return Status::Fail("Missing owned web source context");
    for (const auto& source : results->array)
      sources.try_emplace(source.GetString("url"), source);
  }
  struct Citation {
    size_t item, content;
    io::Json annotation;
  };
  std::vector<Citation> pending;
  size_t bytes = 0;
  for (size_t i = 0; i < Items().array.size(); ++i) {
    const auto& item = Items().array[i];
    const auto* content = item.GetArray("content");
    if (closed_items_.contains(i) || item.GetString("type") != "message" ||
        !content)
      continue;
    for (size_t j = 0; j < content->array.size(); ++j) {
      const auto& part = content->array[j];
      if (part.GetString("type") != "output_text") continue;
      const auto text = part.GetString("text");
      const auto* prior = part.GetArray("annotations");
      std::set<std::pair<std::string, int64_t>> seen;
      if (prior)
        for (const auto& annotation : prior->array)
          if (annotation.GetString("type") == "url_citation")
            seen.emplace(annotation.GetString("url"),
                         annotation.GetInt("start_index"));
      for (const auto& [url, source] : sources) {
        const auto marker = WebCitationMarker(url);
        size_t at = 0;
        while ((at = text.find(marker, at)) != text.npos) {
          const auto start =
              Utf8Characters(std::string_view(text).substr(0, at));
          at += marker.size();
          if (!seen.emplace(url, start).second) continue;
          auto annotation =
              Object({{"type", String("url_citation")},
                      {"url", String(url)},
                      {"title", String(source.GetString("title"))},
                      {"start_index", Number(start)},
                      {"end_index", Number(start + Utf8Characters(marker))}});
          const auto size =
              OutputJson(annotation).size() + 8 * sizeof(io::Json);
          if (size > limit_ - std::min(bytes, limit_))
            return Status::Fail("Web citation capacity exhausted");
          bytes += size;
          pending.push_back({i, j, std::move(annotation)});
        }
      }
    }
  }
  auto status = Charge(bytes);
  if (!status) return status;
  for (auto& citation : pending) {
    auto& annotations = Field(Field(Items().array[citation.item], "content")
                                  .array[citation.content],
                              "annotations")
                            .array;
    auto fields = Location(citation.item, true);
    Set(&fields, "content_index", Number(citation.content));
    Set(&fields, "annotation_index", Number(annotations.size()));
    Set(&fields, "annotation", citation.annotation);
    annotations.push_back(std::move(citation.annotation));
    Event("response.output_text.annotation.added", std::move(fields), wire);
  }
  return {};
}
}  // namespace q4t::server
