#include "mcp_provider_graph.h"

#include <algorithm>
#include <chrono>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/native_crypto.h"

namespace q4t::server::mcp_detail::graph_detail {
namespace {
using namespace object_detail;
using namespace provider_detail;
constexpr std::string_view kPrefix = "q4t-graph-1.";
int64_t Now() {
  return std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}
struct Key {
  CryptoKey bytes;
  explicit Key(std::string_view authority)
      : bytes(trace::Sha256({reinterpret_cast<const uint8_t*>(authority.data()),
                             authority.size()})) {}
  ~Key() { CryptoWipe(bytes); }
};
ObjectReply Unavailable() {
  return Error(503, "mcp_provider_cursor_unavailable",
               "Provider continuation protection is unavailable");
}
std::string Aad(std::string_view path, const CursorContext& context) {
  return OutputJson(Object({{"purpose", String(std::string(kPrefix))},
                            {"path", String(std::string(path))},
                            {"binding", String(context.binding)}}));
}
bool Decode(std::string_view raw, std::string* value) {
  value->clear();
  auto hex = [](char c) {
    return c >= '0' && c <= '9'   ? c - '0'
           : c >= 'a' && c <= 'f' ? c - 'a' + 10
           : c >= 'A' && c <= 'F' ? c - 'A' + 10
                                  : -1;
  };
  for (size_t i = 0; i < raw.size(); ++i) {
    unsigned char c = raw[i];
    if (c == '%') {
      if (i + 2 >= raw.size() || hex(raw[i + 1]) < 0 || hex(raw[i + 2]) < 0)
        return false;
      c = hex(raw[i + 1]) * 16 + hex(raw[i + 2]);
      i += 2;
    }
    if (c < 32 || c == 127) return false;
    *value += c;
  }
  return !value->empty() && *value != "." && *value != "..";
}
// Decode each segment once, retaining encoded slashes as identifier bytes.
// OData's collection('id') is equivalent to collection/id, not a different
// resource. Only known route keywords are case folded by the route matcher.
bool Segments(std::string_view path, std::vector<std::string>* segments) {
  if (!path.starts_with('/') || path.size() > 8192) return false;
  path.remove_prefix(1);
  while (!path.empty()) {
    const auto slash = path.find('/');
    auto raw = path.substr(0, slash);
    std::string item;
    if (!Decode(raw, &item)) return false;
    const auto open = item.find("('");
    if (raw.find('(') != raw.npos && raw.ends_with(')') && open != item.npos &&
        open > 0 && item.ends_with("')")) {
      auto id = std::string_view(item).substr(open + 2, item.size() - open - 4);
      std::string unquoted;
      for (size_t i = 0; i < id.size(); ++i) {
        if (id[i] == '\'' && (++i == id.size() || id[i] != '\'')) return false;
        unquoted += id[i];
      }
      if (unquoted.empty() || unquoted == "." || unquoted == "..") return false;
      segments->push_back(item.substr(0, open));
      segments->push_back(std::move(unquoted));
    } else {
      segments->push_back(std::move(item));
    }
    if (segments->size() > 12) return false;
    if (slash == path.npos) break;
    path.remove_prefix(slash + 1);
    if (path.empty()) return false;
  }
  return !segments->empty();
}
bool Word(std::string_view a, std::string_view b) {
  if (a.size() != b.size()) return false;
  for (size_t i = 0; i < a.size(); ++i) {
    auto lower = [](unsigned char c) {
      return c >= 'A' && c <= 'Z' ? c + 32 : c;
    };
    if (lower(a[i]) != lower(b[i])) return false;
  }
  return true;
}
bool Exact(const std::vector<std::string>& actual,
           const std::vector<std::string>& expected) {
  if (actual.size() != expected.size()) return false;
  bool identifier = false;
  for (size_t i = 0; i < expected.size(); ++i) {
    if (identifier ? actual[i] != expected[i] : !Word(actual[i], expected[i]))
      return false;
    identifier =
        !identifier &&
        (Word(expected[i], "users") || Word(expected[i], "mailFolders") ||
         Word(expected[i], "calendars") || Word(expected[i], "chats"));
  }
  return true;
}
}  // namespace
bool CursorPath(std::string_view actual, std::string_view expected,
                bool authenticated_alias) {
  std::vector<std::string> a, e;
  if (!Segments(actual, &a) || !Segments(expected, &e)) return false;
  if (Exact(a, e)) return true;
  if (!authenticated_alias || e.size() < 2 || !Word(e[0], "me")) return false;
  // An authenticated Graph response may canonicalize /me to /users/{id}.
  // Raw client URLs never use this branch. The sealed continuation also binds
  // the original folder/calendar ID, query and time window in its AAD.
  if (a.size() >= 3 && Word(a[0], "users")) {
    a.erase(a.begin(), a.begin() + 2);
    a.insert(a.begin(), "me");
  }
  if (Exact(a, e)) return true;
  if (a.empty() || !Word(a[0], "me") || !Word(a.back(), e.back())) return false;
  if (e.size() == 4 && Word(e[1], "mailFolders") && Word(e[3], "messages"))
    return a.size() == 4 && Word(a[1], "mailFolders");
  if ((e.size() == 3 && Word(e[1], "calendar")) ||
      (e.size() == 4 && Word(e[1], "calendars"))) {
    if (!Word(e.back(), "events") && !Word(e.back(), "calendarView"))
      return false;
    // A selected calendar may receive its canonical ID, but only the default
    // calendar may collapse to /me/events or /me/calendarView.
    return (a.size() == 4 && Word(a[1], "calendars")) ||
           (e.size() == 3 &&
            ((a.size() == 3 && Word(a[1], "calendar")) || a.size() == 2));
  }
  return false;
}
ObjectReply SealCursor(std::string_view url, std::string_view path,
                       const CursorContext& context, std::string* token) {
  if (!token || context.authority.empty() || context.binding.empty() ||
      url.empty() || url.size() > 8192 || context.limit < 0 ||
      context.limit > 1000)
    return Protocol();
  Key key(context.authority);
  CryptoNonce nonce;
  CryptoTag tag;
  std::string encrypted;
  if (CryptoRandom(nonce) != CryptoResult::kOk ||
      CryptoSeal(key.bytes, nonce, Aad(path, context),
                 OutputJson(Object({{"url", String(std::string(url))},
                                    {"limit", Number(context.limit)},
                                    {"issued_at", Number(Now())}})),
                 &encrypted, &tag) != CryptoResult::kOk)
    return Unavailable();
  std::string frame(reinterpret_cast<const char*>(nonce.data()), nonce.size());
  frame.append(reinterpret_cast<const char*>(tag.data()), tag.size());
  frame += encrypted;
  auto encoded = Base64(frame);
  for (auto& c : encoded) {
    if (c == '+')
      c = '-';
    else if (c == '/')
      c = '_';
  }
  while (!encoded.empty() && encoded.back() == '=') encoded.pop_back();
  if (encoded.size() + kPrefix.size() > 16384) return Capacity();
  *token = std::string(kPrefix) + encoded;
  return {};
}
ObjectReply OpenCursor(std::string_view token, std::string_view path,
                       CursorContext* context, std::string* url) {
  if (!context || context->authority.empty() || context->binding.empty() ||
      !url || !token.starts_with(kPrefix) || token.size() > 16384)
    return Invalid();
  std::string frame;
  if (!DecodeBase64Url(token.substr(kPrefix.size()), &frame).ok() ||
      frame.size() <= 28)
    return Invalid();
  CryptoNonce nonce;
  CryptoTag tag;
  std::copy_n(reinterpret_cast<const uint8_t*>(frame.data()), nonce.size(),
              nonce.begin());
  std::copy_n(reinterpret_cast<const uint8_t*>(frame.data()) + nonce.size(),
              tag.size(), tag.begin());
  Key key(context->authority);
  std::string plain;
  const auto opened = CryptoOpen(
      key.bytes, nonce, Aad(path, *context),
      std::string_view(frame).substr(nonce.size() + tag.size()), tag, &plain);
  if (opened == CryptoResult::kUnavailable || opened == CryptoResult::kFailure)
    return Unavailable();
  if (opened != CryptoResult::kOk) return Invalid();
  Json payload;
  if (!io::ParseJson(plain, &payload, true,
                     {.max_depth = 4,
                      .max_values = 16,
                      .max_string_bytes = 8192,
                      .reject_duplicate_keys = true}) ||
      !payload.IsObject())
    return Invalid();
  const auto* limit = payload.Find("limit");
  const auto* issued = payload.Find("issued_at");
  const auto uri = payload.GetString("url");
  if (!limit || !issued || !IsInputInteger(*limit) ||
      !IsInputInteger(*issued) || limit->number < 0 || limit->number > 1000 ||
      issued->number < Now() - 3600 || issued->number > Now() + 60 ||
      uri.empty() || uri.size() > 8192 ||
      (context->explicit_limit && context->limit != limit->AsInt()))
    return Invalid();
  context->limit = limit->AsInt();
  *url = uri;
  return {};
}
}  // namespace q4t::server::mcp_detail::graph_detail
