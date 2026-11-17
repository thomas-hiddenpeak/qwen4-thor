#include "q4t/server/web_index.h"

#include <algorithm>
#include <ctime>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "web_page_internal.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kPages = 32, kDataBytes = 8U << 20;
bool Hex(std::string_view value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
std::string Id(std::string_view policy) {
  return "webidx_" + std::string(policy);
}
ObjectReply Invalid() {
  return Error(503, "web_index_invalid", "Web index snapshot is inconsistent");
}
ObjectReply Capacity() {
  return Error(413, "web_index_capacity", "Web index capacity exceeded");
}
ObjectReply Owner(const BinaryAccess& access, std::string_view policy) {
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  if (!Hex(policy))
    return Error(500, "web_index_policy_invalid",
                 "Invalid web index policy identity");
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Web index access cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Web index access timed out");
  return {};
}
bool Url(std::string_view value) {
  file_detail::Url parsed;
  return file_detail::ParseUrl(value, &parsed) && parsed.Text() == value;
}
bool ValidPage(const WebPage& page) {
  WebPage checked;
  return Url(page.url) && Hex(page.source_sha256) &&
         web_detail::Page(web_detail::Page(page), {}, &checked);
}
ObjectRequest Request(const BinaryAccess& access, std::string_view policy) {
  ObjectRequest request;
  request.kind = ObjectKind::kToolRun;
  request.id = Id(policy);
  request.scope = access.scope;
  return request;
}
ObjectReply Snapshot(const Json& record, const BinaryAccess& access,
                     std::string_view policy, WebIndexSnapshot* output) {
  const auto* data = record.Find("data");
  const auto* pages = data ? data->GetArray("pages") : nullptr;
  if (!data || !data->IsObject() ||
      data->GetString("format") != "q4t.web.index.v1" ||
      data->GetString("policy_sha256") != policy ||
      data->GetString("scope_sha256") != Digest(access.scope) || !pages ||
      pages->array.size() > kPages || record.GetString("id") != Id(policy) ||
      record.GetInt("revision") < 1 || record.GetInt("expires_at") < 1 ||
      data->GetInt("observed_at") < 1 ||
      data->GetInt("observed_at") >= record.GetInt("expires_at"))
    return Invalid();
  if (OutputJson(*data).size() > kDataBytes) return Capacity();
  WebIndexSnapshot next;
  next.id = record.GetString("id");
  next.policy_sha256 = std::string(policy);
  next.revision = record.GetInt("revision");
  next.observed_at = data->GetInt("observed_at");
  next.expires_at = record.GetInt("expires_at");
  std::set<std::string> urls;
  for (const auto& value : pages->array) {
    if (auto result = Owner(access, policy); !result.ok()) return result;
    WebPage page;
    if (!web_detail::Page(value, {}, &page) || !Url(page.url) ||
        !Hex(page.source_sha256) || !urls.insert(page.url).second)
      return Invalid();
    next.pages.push_back(std::move(page));
  }
  if (next.expires_at <= std::time(nullptr))
    return Error(404, "web_index_unavailable", "Web index snapshot expired");
  *output = std::move(next);
  return {};
}
ObjectReply Receipt(ObjectReply result, std::string_view policy) {
  if (result.commit_unknown)
    result.value = Object({{"id", String(Id(policy))}});
  return result;
}
}  // namespace

ObjectReply WebIndexStore::Read(const BinaryAccess& access,
                                std::string_view policy_sha256,
                                WebIndexSnapshot* output) {
  if (!output) return Error(500, "web_index_state_invalid", "Missing output");
  *output = {};
  if (auto owner = Owner(access, policy_sha256); !owner.ok()) return owner;
  auto result = objects_.Execute(Request(access, policy_sha256),
                                 access.deadline, access.cancelled);
  if (!result.ok()) {
    if (result.code == "object_not_found")
      return Error(404, "web_index_unavailable",
                   "No retained web index snapshot");
    return result;
  }
  return Snapshot(result.value, access, policy_sha256, output);
}

ObjectReply WebIndexStore::Publish(const BinaryAccess& access,
                                   std::string_view policy_sha256,
                                   std::span<const WebPageReceipt> pages,
                                   int64_t expected_revision, int ttl_seconds,
                                   WebIndexSnapshot* output) {
  if (!output) return Error(500, "web_index_state_invalid", "Missing output");
  *output = {};
  if (auto owner = Owner(access, policy_sha256); !owner.ok()) return owner;
  if (expected_revision < 0 || ttl_seconds < 1 || ttl_seconds > 30 * 24 * 3600)
    return Error(400, "web_index_policy_invalid",
                 "Invalid index revision or TTL");
  if (pages.size() > kPages) return Capacity();
  auto values = Array();
  std::set<std::string> urls;
  size_t remaining = kDataBytes - 1024;
  for (const auto& receipt : pages) {
    if (auto owner = Owner(access, policy_sha256); !owner.ok()) return owner;
    const auto& page = receipt.page;
    const auto& source = receipt.source;
    if (!ValidPage(page) || !urls.insert(page.url).second ||
        source.urls.empty() || source.urls.size() > 11 ||
        source.urls.back() != page.url ||
        !std::all_of(source.urls.begin(), source.urls.end(), Url) ||
        source.content.bytes.size() > WebPageLimits{}.max_source_bytes ||
        source.content.sha256 != page.source_sha256 ||
        Digest(source.content.bytes) != page.source_sha256)
      return Invalid();
    auto value = web_detail::Page(page);
    const auto size = OutputJson(value).size();
    if (size + 1 > remaining) return Capacity();
    remaining -= size + 1;
    values.array.push_back(std::move(value));
  }
  auto request = Request(access, policy_sha256);
  request.operation = ObjectOperation::kPut;
  request.expected_revision = expected_revision;
  request.ttl_seconds = ttl_seconds;
  const auto now = std::time(nullptr);
  request.data = Object({{"format", String("q4t.web.index.v1")},
                         {"policy_sha256", String(std::string(policy_sha256))},
                         {"scope_sha256", String(Digest(access.scope))},
                         {"observed_at", Number(now)},
                         {"pages", std::move(values)}});
  auto result = objects_.Execute(request, access.deadline, access.cancelled);
  if (!result.ok()) return Receipt(std::move(result), policy_sha256);
  return Snapshot(result.value, access, policy_sha256, output);
}

ObjectReply WebIndexStore::Erase(const BinaryAccess& access,
                                 std::string_view policy_sha256,
                                 int64_t expected_revision) {
  if (auto owner = Owner(access, policy_sha256); !owner.ok()) return owner;
  if (expected_revision < 1)
    return Error(400, "web_index_policy_invalid",
                 "Expected published revision");
  auto request = Request(access, policy_sha256);
  request.operation = ObjectOperation::kErase;
  request.expected_revision = expected_revision;
  return Receipt(objects_.Execute(request, access.deadline, access.cancelled),
                 policy_sha256);
}
}  // namespace q4t::server
