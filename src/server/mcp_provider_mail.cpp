#include "mcp_provider_mail.h"

#include <iconv.h>

#include <algorithm>
#include <array>
#include <cerrno>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
using namespace provider_detail;
std::string Lower(std::string value) {
  for (auto& c : value)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return value;
}
std::string Header(const Json& part, std::string_view name) {
  const auto* headers = part.GetArray("headers");
  if (!headers) return {};
  for (const auto& item : headers->array)
    if (Lower(item.GetString("name")) == name) return item.GetString("value");
  return {};
}
ObjectReply TextBody(const Json& part, std::string_view bytes,
                     std::string* result) {
  std::string charset = "utf-8";
  const auto header = Lower(Header(part, "content-type"));
  size_t cursor = header.find(';');
  while (cursor != header.npos) {
    auto param = std::string_view(header).substr(cursor + 1);
    const auto end = param.find(';');
    if (end != param.npos) param = param.substr(0, end);
    while (!param.empty() && (param.front() == ' ' || param.front() == '\t'))
      param.remove_prefix(1);
    while (!param.empty() && (param.back() == ' ' || param.back() == '\t'))
      param.remove_suffix(1);
    const auto equal = param.find('=');
    if (equal != param.npos) {
      auto name = param.substr(0, equal);
      while (!name.empty() && (name.back() == ' ' || name.back() == '\t'))
        name.remove_suffix(1);
      if (name == "charset") {
        param.remove_prefix(equal + 1);
        while (!param.empty() &&
               (param.front() == ' ' || param.front() == '\t'))
          param.remove_prefix(1);
        if (param.size() >= 2 && param.front() == '"' && param.back() == '"') {
          param.remove_prefix(1);
          param.remove_suffix(1);
        }
        charset = param;
        break;
      }
    }
    cursor = end == param.npos ? header.npos : header.find(';', cursor + 1);
  }
  if (charset.empty() || charset.size() > 64 ||
      !std::all_of(charset.begin(), charset.end(), [](unsigned char c) {
        return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' ||
               c == '_' || c == '.';
      }))
    return Protocol();
  const auto converter = iconv_open("UTF-8", charset.c_str());
  if (converter == reinterpret_cast<iconv_t>(-1))
    return Error(503, "mcp_provider_charset_unavailable",
                 "Provider body character conversion unavailable");
  struct Close {
    iconv_t value;
    ~Close() { iconv_close(value); }
  } close{converter};
  std::string out;
  char* input = const_cast<char*>(bytes.data());
  size_t left = bytes.size();
  std::array<char, 16384> buffer;
  while (left) {
    char* target = buffer.data();
    size_t available = buffer.size();
    const auto converted = iconv(converter, &input, &left, &target, &available);
    const int error = errno;
    const auto size = buffer.size() - available;
    if (size > kBytes - out.size()) return Capacity();
    out.append(buffer.data(), size);
    if (converted == size_t(-1) && error != E2BIG) return Protocol();
  }
  *result = std::move(out);
  return {};
}
ObjectReply Part(const Json& raw, std::string_view id, bool attachments,
                 const ProviderFetch& fetch, Json* value, Json* content,
                 size_t* parts, size_t depth = 0) {
  if (!raw.IsObject() || depth > 16 || ++*parts > 128) return Capacity();
  const auto index = *parts;
  const auto mime = Lower(raw.GetString("mimeType"));
  if (mime.empty() || mime.size() > 255) return Protocol();
  if (const auto* headers = raw.Find("headers")) {
    if (!headers->IsArray() || headers->array.size() > 1024) return Protocol();
    for (const auto& header : headers->array)
      if (!header.IsObject() || !header.Find("name") ||
          !header.Find("name")->IsString() || !header.Find("value") ||
          !header.Find("value")->IsString())
        return Protocol();
  }
  *value = raw;
  if (const auto* children = raw.Find("parts")) {
    if (!children->IsArray() || children->array.size() > 128) return Protocol();
    auto items = Array();
    for (const auto& child : children->array) {
      Json item;
      auto reply =
          Part(child, id, attachments, fetch, &item, content, parts, depth + 1);
      if (!reply.ok()) return reply;
      items.array.push_back(std::move(item));
    }
    Set(value, "parts", std::move(items));
  }
  const auto* original = raw.Find("body");
  if (!original) return {};
  if (!original->IsObject()) return Protocol();
  auto body = *original;
  const auto* data = original->Find("data");
  if (data && !data->IsString()) return Protocol();
  const auto* size = original->Find("size");
  if (size && (!IsInputInteger(*size) || size->number < 0)) return Protocol();
  const auto attachment = original->GetString("attachmentId");
  const bool body_text =
      mime.starts_with("text/") && raw.GetString("filename").empty() &&
      !Lower(Header(raw, "content-disposition")).starts_with("attachment");
  const bool has_data = (data && !data->str.empty()) || !attachment.empty() ||
                        (size && size->number > 0);
  std::erase_if(body.object,
                [](const auto& item) { return item.first == "data"; });
  Set(&body, "content_read", Boolean(false));
  if (has_data && (body_text || attachments)) {
    Json downloaded;
    if (!attachment.empty()) {
      if (!Identifier(attachment)) return Protocol();
      auto reply = fetch(
          {"https://gmail.googleapis.com/gmail/v1/users/me/"
           "messages/" +
           ProviderEscape(id) + "/attachments/" + ProviderEscape(attachment)},
          &downloaded);
      if (!reply.ok()) return reply;
      data = downloaded.Find("data");
      if (!data || !data->IsString()) return Protocol();
    }
    if (!data) return Protocol();
    std::string bytes;
    auto reply = DecodeBase64Url(data->str, &bytes);
    if (!reply.ok()) return reply;
    if (size && size->number != static_cast<double>(bytes.size()))
      return Protocol();
    const auto uri = "q4t-provider://gmail/" + ProviderEscape(id) + "/part/" +
                     std::to_string(index);
    if (body_text) {
      std::string text;
      reply = TextBody(raw, bytes, &text);
      if (!reply.ok()) return reply;
      content->array.push_back(
          Object({{"type", String("resource")},
                  {"resource", Object({{"uri", String(uri)},
                                       {"mimeType", String(mime)},
                                       {"text", String(std::move(text))}})}}));
    } else {
      content->array.push_back(Resource(uri, mime, Base64(bytes)));
    }
    Set(&body, "content_read", Boolean(true));
    Set(&body, "content_uri", String(uri));
  }
  Set(value, "body", std::move(body));
  return {};
}
}  // namespace
ObjectReply GmailMessage(const Json& response, std::string_view id, bool full,
                         bool attachments, const ProviderFetch& fetch,
                         Json* message, Json* content) {
  if (!response.IsObject() || response.GetString("id") != id) return Protocol();
  const auto* payload = response.Find("payload");
  if (!payload || !payload->IsObject()) return Protocol();
  *message = response;
  if (full) {
    size_t parts = 0;
    Json parsed;
    auto reply =
        Part(*payload, id, attachments, fetch, &parsed, content, &parts);
    if (!reply.ok()) return reply;
    Set(message, "payload", std::move(parsed));
  }
  for (const auto* header : {"subject", "from", "to", "cc", "bcc", "date"}) {
    const auto value = Header(*payload, header);
    if (!value.empty()) Set(message, header, String(value));
  }
  return {};
}
}  // namespace q4t::server::mcp_detail
