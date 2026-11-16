#include "q4t/server/responses_web_search.h"

#include <algorithm>
#include <map>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/input_snapshot_bindings.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kBytes = 8U << 20;
ObjectReply Invalid() {
  return Error(503, "web_context_invalid",
               "Stored web source context is inconsistent");
}
std::string Identity(const Json& item) {
  if (item.GetString("type") != "web_search_call" ||
      item.GetString("status") != "completed" || !ValidId(item.GetString("id")))
    return {};
  auto value = item;
  if (value.Has("action"))
    std::erase_if(Field(value, "action").object,
                  [](const auto& field) { return field.first == "sources"; });
  std::string identity;
  return InputValueIdentity(value, &identity) ? identity : std::string();
}
bool Context(const Json& context) {
  const auto* results = context.GetArray("results");
  if (!context.IsObject() || !results || results->array.size() > 16 ||
      OutputJson(context).size() > (1U << 20))
    return false;
  for (const auto& result : results->array) {
    file_detail::Url url;
    if (!result.IsObject() ||
        !file_detail::ParseUrl(result.GetString("url"), &url) ||
        result.GetString("source_sha256").size() != 64 ||
        result.GetString("citation_marker") != WebCitationMarker(url.Text()))
      return false;
  }
  return true;
}
}  // namespace

ObjectReply MergeWebContexts(const Json& incoming, const Json& items,
                             Json* output) {
  if (!output || !items.IsArray() || items.array.size() > 4096)
    return Invalid();
  std::map<std::string, std::string> identities;
  for (const auto& item : items.array) {
    const auto digest = Identity(item);
    if (digest.empty()) continue;
    auto [it, inserted] = identities.emplace(item.GetString("id"), digest);
    if (!inserted && it->second != digest) return Invalid();
  }
  auto selected = Array();
  std::map<std::string, std::string> seen;
  size_t bytes = 0;
  for (const auto* source : {&incoming, static_cast<const Json*>(output)}) {
    if (source->IsNull()) continue;
    if (!source->IsArray() || source->array.size() > 512) return Invalid();
    for (const auto& binding : source->array) {
      const auto* context = binding.Find("context");
      const auto id = binding.GetString("item_id");
      const auto digest = binding.GetString("item_sha256");
      if (!binding.IsObject() || binding.object.size() != 3 || !ValidId(id) ||
          digest.size() != 64 || !context || !Context(*context))
        return Invalid();
      const auto item = identities.find(id);
      if (item == identities.end()) continue;
      if (item->second != digest) return Invalid();
      std::string identity;
      if (!InputValueIdentity(binding, &identity)) return Invalid();
      const auto [prior, inserted] = seen.emplace(id, identity);
      if (!inserted) {
        if (prior->second != identity) return Invalid();
        continue;
      }
      const auto size = OutputJson(binding).size();
      if (size > kBytes - bytes || selected.array.size() == 512)
        return Error(413, "web_context_capacity",
                     "Retained web context capacity exceeded");
      bytes += size;
      selected.array.push_back(binding);
    }
  }
  *output = std::move(selected);
  return {};
}

ObjectReply BindWebContext(const Json& item, const WebExecutionResult& result,
                           Json* bindings) {
  const auto digest = Identity(item);
  if (!bindings || digest.empty() || !result.context.GetArray("results"))
    return Invalid();
  auto context = result.context;
  for (auto& value : Field(context, "results").array) {
    const auto source = std::find_if(
        result.sources.begin(), result.sources.end(),
        [&](const auto& page) { return page.url == value.GetString("url"); });
    if (source == result.sources.end() ||
        source->source_sha256 != value.GetString("source_sha256") ||
        source->title != value.GetString("title"))
      return Invalid();
    Set(&value, "citation_marker", String(WebCitationMarker(source->url)));
  }
  if (!Context(context)) return Invalid();
  auto next = bindings->IsNull() ? Array() : *bindings;
  if (!next.IsArray()) return Invalid();
  const auto bound = Object({{"item_id", String(item.GetString("id"))},
                             {"item_sha256", String(digest)},
                             {"context", std::move(context)}});
  for (const auto& old : next.array)
    if (old.GetString("item_id") == item.GetString("id")) {
      std::string before, after;
      return InputValueIdentity(old, &before) &&
                     InputValueIdentity(bound, &after) && before == after
                 ? ObjectReply{}
                 : Invalid();
    }
  next.array.push_back(bound);
  if (next.array.size() > 512 || OutputJson(next).size() > kBytes)
    return Error(413, "web_context_capacity",
                 "Retained web context capacity exceeded");
  *bindings = std::move(next);
  return {};
}

const Json* FindWebContext(const Json& bindings, const Json& item) {
  const auto digest = Identity(item);
  if (digest.empty() || !bindings.IsArray()) return nullptr;
  for (const auto& binding : bindings.array)
    if (binding.GetString("item_id") == item.GetString("id") &&
        binding.GetString("item_sha256") == digest)
      return binding.Find("context");
  return nullptr;
}
}  // namespace q4t::server
