#include "q4t/server/responses_output.h"

#include <algorithm>
#include <charconv>
#include <set>
#include <unordered_map>

#include "q4t/server/output_schema.h"
#include "q4t/server/completion_response.h"
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
namespace {
std::string SandboxPath(std::string_view uri) {
  std::string path;
  for (size_t i = 0; i < uri.size(); ++i) {
    if (uri[i] != '%') {
      path.push_back(uri[i]);
      continue;
    }
    if (uri.size() - i < 3) return {};
    unsigned byte = 0;
    const char* start = uri.data() + i + 1;
    const auto result = std::from_chars(start, start + 2, byte, 16);
    if (result.ec != std::errc{} || result.ptr != start + 2) return {};
    path.push_back(static_cast<char>(byte));
    i += 2;
  }
  return path;
}
}  // namespace

Status ResponsesOutput::CitePythonFiles(const ContainerFileExport& export_file,
                                        std::string* wire) {
  wire->clear();
  if (!started_ || terminal_) return Status::Fail("Response is inactive");
  if (python_files_.empty()) return {};
  constexpr std::string_view prefix = "sandbox:/mnt/data/";
  for (size_t i = python_item_ + 1; i < Items().array.size(); ++i) {
    auto& item = Items().array[i];
    if (closed_items_.contains(i) || item.GetString("type") != "message")
      continue;
    auto& content = Field(item, "content").array;
    if (content.size() != 1 || content[0].GetString("type") != "output_text")
      continue;
    const std::string_view text = content[0].Find("text")->str;
    auto& annotations = Field(content[0], "annotations").array;
    std::set<int64_t> existing;
    for (const auto& ref : annotations)
      if (ref.GetString("type") == "container_file_citation")
        existing.insert(ref.GetInt("start_index"));
    size_t offset = 0, characters = 0;
    std::set<std::string> exported;
    while (offset < text.size()) {
      const auto start = text.find(prefix, offset);
      if (start == text.npos) break;
      const auto delimiter =
          text.find_first_of(")>`\"' \t\r\n", start + prefix.size());
      const auto end = delimiter == text.npos ? text.size() : delimiter;
      characters += Utf8Characters(text.substr(offset, start - offset));
      const auto first = characters;
      characters += Utf8Characters(text.substr(start, end - start));
      offset = end;
      const auto encoded = text.substr(start + 8, end - start - 8);
      // The workspace enforces a 1024-byte relative name.
      if (encoded.size() > 3 * 1024 + 10 || existing.contains(first)) continue;
      auto it = python_files_.find(SandboxPath(encoded));
      if (it == python_files_.end()) continue;
      const auto& file = it->second;
      const auto& path = it->first;
      auto annotation =
          Object({{"type", String("container_file_citation")},
                  {"container_id", *file.Find("container_id")},
                  {"file_id", *file.Find("id")},
                  {"filename", String(path.substr(path.rfind('/') + 1))},
                  {"start_index", Number(first)},
                  {"end_index", Number(characters)}});
      auto charged = Charge(OutputJson(annotation).size() + 8 * sizeof(io::Json));
      if (!charged) return charged;
      if (export_file && exported.insert(file.GetString("id")).second) {
        auto saved = export_file(item.GetString("id"), file);
        if (!saved) return saved;
      }
      auto fields = Location(i, true);
      Set(&fields, "annotation_index", Number(annotations.size()));
      Set(&fields, "annotation", annotation);
      annotations.push_back(std::move(annotation));
      Event("response.output_text.annotation.added", std::move(fields), wire);
    }
  }
  return {};
}
}  // namespace q4t::server
