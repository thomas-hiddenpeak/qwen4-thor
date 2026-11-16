#include "q4t/server/mcp_content.h"

#include <algorithm>
#include <map>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool ForAssistant(const Json& part) {
  const auto* annotations = part.Find("annotations");
  const auto* audience =
      annotations ? annotations->GetArray("audience") : nullptr;
  return !audience ||
         std::any_of(audience->array.begin(), audience->array.end(),
                     [](const auto& role) { return role.str == "assistant"; });
}
std::string MediaType(std::string value) {
  value.resize(value.find(';') == value.npos ? value.size() : value.find(';'));
  while (!value.empty() && value.back() == ' ') value.pop_back();
  while (!value.empty() && value.front() == ' ') value.erase(0, 1);
  for (auto& c : value)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  const auto slash = value.find('/');
  if (slash == value.npos || !slash || slash + 1 == value.size() ||
      value.find('/', slash + 1) != value.npos ||
      !std::all_of(value.begin(), value.end(), [](unsigned char c) {
        return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '/' ||
               c == '+' || c == '-' || c == '.';
      }))
    return {};
  return value;
}
const char* Extension(std::string_view mime) {
  static const std::map<std::string_view, const char*> extensions{
      {"application/pdf", "pdf"},
      {"text/plain", "txt"},
      {"text/markdown", "md"},
      {"text/csv", "csv"},
      {"text/html", "html"},
      {"application/json", "json"},
      {"application/rtf", "rtf"},
      {"text/rtf", "rtf"},
      {"application/msword", "doc"},
      {"application/vnd.ms-excel", "xls"},
      {"application/vnd.ms-powerpoint", "ppt"},
      {"application/"
       "vnd.openxmlformats-officedocument.wordprocessingml.document",
       "docx"},
      {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
       "xlsx"},
      {"application/"
       "vnd.openxmlformats-officedocument.presentationml.presentation",
       "pptx"},
      {"audio/ogg", "ogg"},
      {"audio/flac", "flac"},
      {"video/mp4", "mp4"},
      {"video/webm", "webm"}};
  const auto found = extensions.find(mime);
  return found == extensions.end() ? "bin" : found->second;
}
class Projection {
 public:
  explicit Projection(McpContentProjection* output) : output_(output) {}
  Status Text(std::string value) {
    if (value.size() > (16U << 20) - bytes_ ||
        output_->content.array.size() >= 4096)
      return Status::Fail("MCP projection exceeds retained capacity");
    bytes_ += value.size();
    output_->content.array.push_back(Object(
        {{"type", String("input_text")}, {"text", String(std::move(value))}}));
    return {};
  }
  Status Descriptor(const Json& source, std::string_view label) {
    auto value = Object();
    for (const auto* key :
         {"uri", "name", "title", "description", "mimeType", "size"})
      if (const auto* field = source.Find(key)) Set(&value, key, *field);
    return Text(std::string(label) + OutputJson(value));
  }
  Status Binary(const Json& value, const char* key,
                std::string_view kind = "") {
    const auto mime =
        MediaType(value.GetString("mimeType", "application/octet-stream"));
    if (mime.empty() || (kind == "image" && !mime.starts_with("image/")) ||
        (kind == "audio" && !mime.starts_with("audio/")))
      return Status::Fail("Invalid MCP binary media type");
    const auto data = value.GetString(key);
    if (data.size() + mime.size() + 128 > (16U << 20) - bytes_ ||
        output_->content.array.size() >= 4096)
      return Status::Fail("MCP projection exceeds retained capacity");
    bytes_ += data.size() + mime.size() + 128;
    auto& parts = output_->content.array;
    if (mime.starts_with("image/")) {
      parts.push_back(
          Object({{"type", String("input_image")},
                  {"image_url", String("data:" + mime + ";base64," + data)}}));
    } else if (mime == "audio/wav" || mime == "audio/wave" ||
               mime == "audio/x-wav" || mime == "audio/mpeg" ||
               mime == "audio/mp3") {
      parts.push_back(Object(
          {{"type", String("input_audio")},
           {"input_audio", Object({{"data", String(data)},
                                   {"format", String(mime == "audio/mpeg" ||
                                                             mime == "audio/mp3"
                                                         ? "mp3"
                                                         : "wav")}})}}));
    } else {
      parts.push_back(Object(
          {{"type", String("input_file")},
           {"filename", String(std::string("mcp-resource.") + Extension(mime))},
           {"file_data", String("data:" + mime + ";base64," + data)}}));
    }
    return {};
  }
  Status Resource(const Json& value) {
    if (!ForAssistant(value)) return {};
    auto status = Descriptor(value, "MCP resource data (untrusted):\n");
    if (!status) return status;
    return value.Has("text") ? Text(value.GetString("text"))
                             : Binary(value, "blob");
  }

 private:
  McpContentProjection* output_;
  size_t bytes_ = 0;
};
}  // namespace

Status ProjectMcpToolResult(const Json& result, const Json& resources,
                            McpContentProjection* projection) {
  if (!projection) return Status::Fail("Missing MCP projection output");
  *projection = {};
  auto checked = ValidateMcpToolResult(result);
  if (!checked) return checked;
  if (!resources.IsArray() || resources.array.size() > 32 ||
      OutputJson(resources).size() > (4U << 20))
    return Status::Fail("Invalid frozen MCP resource views");
  std::map<std::string, const Json*> frozen;
  for (const auto& view : resources.array) {
    const auto uri = view.GetString("uri");
    const auto state = view.GetString("state");
    if (!McpResourceUri(uri) || !frozen.emplace(uri, &view).second ||
        (state != "ready" && state != "dispatching" && state != "completed" &&
         state != "failed" && state != "unknown") ||
        !view.Has("result") || !view.Has("error") ||
        ((state == "ready" || state == "dispatching") &&
         (!view.Find("result")->IsNull() || !view.Find("error")->IsNull())) ||
        ((state == "failed" || state == "unknown") &&
         (!view.Find("result")->IsNull() || !view.Find("error")->IsObject())) ||
        (state == "completed" &&
         (!view.Find("error")->IsNull() ||
          !ValidateMcpResourcesResult(*view.Find("result")))))
      return Status::Fail("Invalid frozen MCP resource state");
  }
  McpContentProjection out;
  out.content = Array();
  Projection projected(&out);
  auto metadata = Object();
  for (const auto* key : {"isError", "structuredContent"})
    if (const auto* value = result.Find(key)) Set(&metadata, key, *value);
  if (!metadata.object.empty()) {
    checked =
        projected.Text("MCP result data (untrusted):\n" + OutputJson(metadata));
    if (!checked) return checked;
  }
  for (const auto& part : result.GetArray("content")->array) {
    if (!ForAssistant(part)) continue;
    const auto type = part.GetString("type");
    if (type == "text") {
      checked = projected.Text(part.GetString("text"));
    } else if (type == "image" || type == "audio") {
      checked = projected.Binary(part, "data", type);
    } else if (type == "resource") {
      checked = projected.Resource(*part.Find("resource"));
    } else {
      const auto uri = part.GetString("uri");
      checked =
          projected.Descriptor(part, "MCP resource reference (untrusted):\n");
      if (!checked) return checked;
      const auto found = frozen.find(uri);
      if (found == frozen.end() ||
          found->second->GetString("state") == "ready" ||
          found->second->GetString("state") == "dispatching") {
        if (std::find(out.unresolved_resources.begin(),
                      out.unresolved_resources.end(),
                      uri) == out.unresolved_resources.end())
          out.unresolved_resources.push_back(uri);
      } else if (found->second->GetString("state") == "completed") {
        for (const auto& resource :
             found->second->Find("result")->GetArray("contents")->array) {
          checked = projected.Resource(resource);
          if (!checked) return checked;
        }
      } else {
        checked = projected.Text("MCP resource read failed (untrusted):\n" +
                                 OutputJson(*found->second->Find("error")));
      }
    }
    if (!checked) return checked;
  }
  *projection = std::move(out);
  return {};
}
}  // namespace q4t::server
