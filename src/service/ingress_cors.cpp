#include "q4t/service/ingress_cors.h"

#include <algorithm>
#include <charconv>

#include "q4t/service/ingress_address.h"
#include "q4t/service/guardian_wire.h"

namespace q4t::service::ingress {
namespace {
bool Token(std::string_view text) {
  if (text.empty() || text.size() > 128) return false;
  for (unsigned char c : text)
    if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-'))
      return false;
  return true;
}
bool Origin(std::string_view value) {
  if (value == "null") return true;
  if (value.size() > 2048) return false;
  if (value.starts_with("https://"))
    value.remove_prefix(8);
  else if (value.starts_with("http://"))
    value.remove_prefix(7);
  else
    return false;
  if (value.empty()) return false;
  size_t colon = value.find(':');
  if (value.front() == '[') {
    const auto end = value.find(']');
    if (end == value.npos ||
        Address::Parse(std::string(value.substr(1, end - 1))).family !=
            AF_INET6)
      return false;
    if (end + 1 == value.size()) return true;
    if (value[end + 1] != ':') return false;
    colon = end + 1;
  } else {
    const auto host = value.substr(0, colon);
    if (host.empty() || host.front() == '.' || host.back() == '.') return false;
    for (unsigned char c : host)
      if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '.'))
        return false;
  }
  if (colon == value.npos) return true;
  unsigned port = 0;
  const auto* begin = value.data() + colon + 1;
  const auto* end = value.data() + value.size();
  const auto result = std::from_chars(begin, end, port);
  return result.ec == std::errc{} && result.ptr == end && port > 0 &&
         port <= 65535;
}
bool List(const io::Json& value, const char* key, size_t lower,
          std::vector<std::string>* out) {
  const auto* array = value.GetArray(key);
  if (!array || array->array.size() < lower || array->array.size() > 32)
    return false;
  for (const auto& member : array->array) {
    if (!member.IsString() || member.str.empty() || member.str.size() > 2048 ||
        std::find(out->begin(), out->end(), member.str) != out->end())
      return false;
    out->push_back(member.str);
  }
  return true;
}
std::string Join(const std::vector<std::string>& items) {
  std::string text;
  for (const auto& item : items) {
    if (!text.empty()) text += ", ";
    text += item;
  }
  return text;
}
}  // namespace

bool Cors::Load(const io::Json& value) {
  if (!wire::HasKeys(value,
                     {"allowed_origins", "allowed_methods", "allowed_headers",
                      "exposed_headers", "allow_credentials", "max_age"}))
    return false;
  const auto* credentials = value.Find("allow_credentials");
  uint64_t age = 0;
  if (!credentials || !credentials->IsBool() ||
      !wire::GetUnsigned(value, "max_age", &age) || age > 86400 ||
      !List(value, "allowed_origins", 1, &origins_) ||
      !List(value, "allowed_methods", 1, &methods_) ||
      !List(value, "allowed_headers", 0, &headers_) ||
      !List(value, "exposed_headers", 0, &exposed_))
    return false;
  credentials_ = credentials->boolean;
  max_age_ = age;
  for (const auto& origin : origins_)
    if (origin == "*" ? credentials_ || origins_.size() != 1 : !Origin(origin))
      return false;
  for (const auto& method : methods_)
    if (method != "GET" && method != "HEAD" && method != "POST" &&
        method != "PUT" && method != "PATCH" && method != "DELETE")
      return false;
  for (const auto& header : headers_)
    if (!Token(header) || header.starts_with("x-q4t-") || header == "host" ||
        header == "content-length" || header == "transfer-encoding" ||
        header == "connection" || header == "cookie")
      return false;
  for (const auto& header : exposed_)
    if (!Token(header) || header.starts_with("x-q4t-") ||
        header == "set-cookie")
      return false;
  return true;
}

bool Cors::Response(std::string_view origin, std::string* headers) const {
  if (!Enabled()) return true;
  *headers = "Vary: Origin\r\n";
  if (origin.empty()) return true;
  if (!Origin(origin)) return false;
  const bool wildcard = origins_.front() == "*";
  if (!wildcard &&
      std::find(origins_.begin(), origins_.end(), origin) == origins_.end())
    return false;
  *headers +=
      "Access-Control-Allow-Origin: " + std::string(wildcard ? "*" : origin) +
      "\r\n";
  if (credentials_) *headers += "Access-Control-Allow-Credentials: true\r\n";
  if (!exposed_.empty())
    *headers += "Access-Control-Expose-Headers: " + Join(exposed_) + "\r\n";
  return true;
}

bool Cors::Preflight(std::string_view method, std::string_view requested,
                     std::string* response) const {
  if (!Enabled() ||
      std::find(methods_.begin(), methods_.end(), method) == methods_.end())
    return false;
  std::vector<std::string> accepted;
  if (!requested.empty()) {
    while (!requested.empty()) {
      const auto comma = requested.find(',');
      auto part = requested.substr(0, comma);
      while (!part.empty() && (part.front() == ' ' || part.front() == '\t'))
        part.remove_prefix(1);
      while (!part.empty() && (part.back() == ' ' || part.back() == '\t'))
        part.remove_suffix(1);
      std::string header(part);
      for (char& c : header)
        if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
      if (!Token(header) ||
          std::find(headers_.begin(), headers_.end(), header) ==
              headers_.end() ||
          accepted.size() >= 32)
        return false;
      if (std::find(accepted.begin(), accepted.end(), header) == accepted.end())
        accepted.push_back(header);
      if (comma == requested.npos) break;
      requested.remove_prefix(comma + 1);
      if (requested.empty()) return false;
    }
  }
  *response +=
      "Vary: Access-Control-Request-Method, Access-Control-Request-Headers\r\n"
      "Access-Control-Allow-Methods: " +
      std::string(method) + "\r\n";
  if (!accepted.empty())
    *response += "Access-Control-Allow-Headers: " + Join(accepted) + "\r\n";
  *response += "Access-Control-Max-Age: " + std::to_string(max_age_) + "\r\n";
  return true;
}
}  // namespace q4t::service::ingress
