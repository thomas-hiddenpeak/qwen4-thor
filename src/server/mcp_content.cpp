#include "q4t/server/mcp_content.h"

#include <algorithm>
#include <cmath>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kResultBytes = 4U << 20;
constexpr size_t kContentItems = 256;
bool Text(const Json& value, const char* key, bool required = false) {
  const auto* field = value.Find(key);
  return field ? field->IsString() : !required;
}
bool Mime(const Json& value, bool required = false) {
  const auto* field = value.Find("mimeType");
  return !field ? !required
                : field->IsString() && !field->str.empty() &&
                      field->str.size() <= 255 &&
                      std::none_of(
                          field->str.begin(), field->str.end(),
                          [](unsigned char c) { return c < 32 || c == 127; });
}
bool Metadata(const Json& value) {
  if (const auto* meta = value.Find("_meta"); meta && !meta->IsObject())
    return false;
  const auto* annotations = value.Find("annotations");
  if (!annotations) return true;
  if (!annotations->IsObject() || !Text(*annotations, "lastModified"))
    return false;
  if (const auto* priority = annotations->Find("priority");
      priority && (!priority->IsNumber() || !std::isfinite(priority->number) ||
                   priority->number < 0 || priority->number > 1))
    return false;
  if (const auto* audience = annotations->Find("audience")) {
    if (!audience->IsArray() || audience->array.size() > kContentItems)
      return false;
    for (const auto& role : audience->array)
      if (!role.IsString() || (role.str != "user" && role.str != "assistant"))
        return false;
  }
  return true;
}
bool Binary(const Json& value, const char* key) {
  const auto* field = value.Find(key);
  if (!field || !field->IsString() || field->str.size() > kResultBytes)
    return false;
  const auto& bytes = field->str;
  if (bytes.empty()) return true;
  if (bytes.size() % 4) return false;
  const auto digit = [](unsigned char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    return c == '+' ? 62 : c == '/' ? 63 : -1;
  };
  size_t padding = bytes.back() == '=' ? 1 : 0;
  if (padding && bytes[bytes.size() - 2] == '=') ++padding;
  const auto digits = bytes.size() - padding;
  for (size_t i = 0; i < digits; ++i)
    if (digit(bytes[i]) < 0) return false;
  // This is a binary contract, not image validation. Audio and document bytes
  // must pass here too; a data URL is not a native MCP base64 payload.
  return !(padding == 2 && (digit(bytes[digits - 1]) & 15)) &&
         !(padding == 1 && (digit(bytes[digits - 1]) & 3));
}
bool Resource(const Json& value) {
  if (!value.IsObject() || !McpResourceUri(value.GetString("uri")) ||
      !Mime(value) || !Metadata(value) ||
      value.Has("text") == value.Has("blob"))
    return false;
  return value.Has("text") ? Text(value, "text", true) : Binary(value, "blob");
}
bool Content(const Json& value) {
  if (!value.IsObject() || !Metadata(value)) return false;
  const auto type = value.GetString("type");
  if (type == "text") return Text(value, "text", true);
  if (type == "image" || type == "audio")
    return Mime(value, true) && Binary(value, "data");
  if (type == "resource") {
    const auto* resource = value.Find("resource");
    return resource && Resource(*resource);
  }
  if (type != "resource_link" || !McpResourceUri(value.GetString("uri")) ||
      !Text(value, "name", true) || !Text(value, "title") ||
      !Text(value, "description") || !Mime(value))
    return false;
  const auto* size = value.Find("size");
  return !size || (IsInputInteger(*size) && size->number >= 0);
}
bool Envelope(const Json& value) {
  return value.IsObject() && OutputJson(value).size() <= kResultBytes &&
         (!value.Has("_meta") || value.Find("_meta")->IsObject());
}
}  // namespace

bool McpResourceUri(std::string_view uri) {
  const auto colon = uri.find(':');
  const auto alpha = [](unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
  };
  if (uri.empty() || uri.size() > 8192 || colon == uri.npos || !colon ||
      !alpha(uri.front()) ||
      std::any_of(uri.begin(), uri.end(),
                  [](unsigned char c) { return c <= 32 || c == 127; }))
    return false;
  for (size_t i = 1; i < colon; ++i) {
    const auto c = static_cast<unsigned char>(uri[i]);
    if (!alpha(c) && !(c >= '0' && c <= '9') && c != '+' && c != '-' &&
        c != '.')
      return false;
  }
  return true;
}

Status ValidateMcpToolResult(const Json& result) {
  const auto* content = result.GetArray("content");
  const auto* structured = result.Find("structuredContent");
  const auto* error = result.Find("isError");
  if (!Envelope(result) || !content || content->array.size() > kContentItems ||
      (structured && !structured->IsObject()) || (error && !error->IsBool()))
    return Status::Fail("Invalid MCP tool result envelope");
  for (const auto& part : content->array)
    if (!Content(part)) return Status::Fail("Invalid MCP native content");
  return {};
}

Status ValidateMcpResourcesResult(const Json& result) {
  const auto* contents = result.GetArray("contents");
  if (!Envelope(result) || !contents || contents->array.size() > kContentItems)
    return Status::Fail("Invalid MCP resources result envelope");
  for (const auto& resource : contents->array)
    if (!Resource(resource))
      return Status::Fail("Invalid MCP resource contents");
  return {};
}
}  // namespace q4t::server
