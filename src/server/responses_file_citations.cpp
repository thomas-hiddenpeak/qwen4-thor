#include "q4t/server/responses_output.h"

#include <algorithm>
#include <set>
#include <unordered_map>

#include "q4t/server/output_schema.h"
#include "responses_json.h"

namespace q4t::server {
using namespace responses_detail;

Status ResponsesOutput::CiteFileSearchSources(std::string* wire) {
  wire->clear();
  if (!started_ || terminal_)
    return Status::Fail("File citations require an active response");
  struct Source {
    std::string filename;
    size_t item = 0;
  };
  std::unordered_map<std::string, Source> sources;
  struct Citation {
    size_t item;
    io::Json annotation;
  };
  std::vector<Citation> pending;
  size_t bytes = 0;
  for (size_t i = 0; i < Items().array.size(); ++i) {
    const auto& item = Items().array[i];
    const auto* results = item.GetArray("results");
    if (item.GetString("type") == "file_search_call" &&
        item.GetString("status") == "completed" && results) {
      for (const auto& result : results->array) {
        const auto id = result.GetString("file_id");
        const auto filename = result.GetString("filename");
        if (!id.empty() && !filename.empty())
          sources.try_emplace(id, Source{filename, i});
      }
      continue;
    }
    const auto* content = item.GetArray("content");
    if (sources.empty() || closed_items_.contains(i) ||
        item.GetString("type") != "message" || !content ||
        content->array.size() != 1 ||
        content->array[0].GetString("type") != "output_text")
      continue;
    const auto& part = content->array[0];
    const auto& text = part.Find("text")->str;
    std::set<std::pair<std::string, int64_t>> existing;
    if (const auto* annotations = part.GetArray("annotations"))
      for (const auto& annotation : annotations->array)
        if (annotation.GetString("type") == "file_citation")
          existing.emplace(annotation.GetString("file_id"),
                           annotation.GetInt("index", -1));
    size_t offset = 0, counted = 0;
    int64_t characters = 0;
    while ((offset = text.find("[file:", offset)) != text.npos) {
      const auto start = offset + 6;
      const auto fragment = std::string_view(text).substr(start, 257);
      const auto close = fragment.find(']');
      offset = start;
      if (close == fragment.npos) continue;
      const auto source = sources.find(std::string(fragment.substr(0, close)));
      if (source == sources.end() || source->second.item >= i) continue;
      const auto end = start + close + 1;
      characters +=
          std::count_if(text.begin() + counted, text.begin() + end,
                        [](unsigned char c) { return (c & 0xc0) != 0x80; });
      counted = offset = end;
      if (!existing.emplace(source->first, characters).second) continue;
      auto annotation = Object({{"type", String("file_citation")},
                                {"file_id", String(source->first)},
                                {"filename", String(source->second.filename)},
                                {"index", Number(characters)}});
      const auto size = OutputJson(annotation).size() + 8 * sizeof(io::Json);
      if (size > limit_ - std::min(bytes, limit_))
        return Status::Fail("File citation capacity exhausted");
      bytes += size;
      pending.push_back({i, std::move(annotation)});
    }
  }
  auto status = Charge(bytes);
  if (!status) return status;
  for (auto& citation : pending) {
    auto& annotations =
        Field(Field(Items().array[citation.item], "content").array[0],
              "annotations")
            .array;
    auto fields = Location(citation.item, true);
    Set(&fields, "annotation_index", Number(annotations.size()));
    Set(&fields, "annotation", citation.annotation);
    annotations.push_back(std::move(citation.annotation));
    Event("response.output_text.annotation.added", std::move(fields), wire);
  }
  return {};
}
}  // namespace q4t::server
