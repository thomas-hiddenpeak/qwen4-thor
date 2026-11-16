#include "q4t/server/web_search.h"

#include <algorithm>
#include <set>

#include "file_fetch_internal.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/input_contract.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Status Invalid(ChatPlanError* error, std::string path, std::string message) {
  *error = {"invalid_request", std::move(path), 400};
  return Status::Fail(std::move(message));
}
Status Domains(const Json& value, std::string path,
               std::vector<std::string>* output, ChatPlanError* error) {
  if (value.IsNull()) return {};
  if (!value.IsArray() || value.array.size() > 100)
    return Invalid(error, path, "Expected at most 100 domains");
  std::set<std::string> unique;
  for (size_t i = 0; i < value.array.size(); ++i) {
    const auto& item = value.array[i];
    file_detail::Url url;
    if (!item.IsString() || item.str.empty() ||
        item.str.find_first_of("/:?#@%[]") != item.str.npos ||
        !file_detail::ParseUrl("https://" + item.str + '/', &url))
      return Invalid(error, path + '[' + std::to_string(i) + ']',
                     "Expected a domain without scheme, port or path");
    auto domain = url.host;
    if (domain.ends_with('.')) domain.pop_back();
    if (unique.insert(domain).second) output->push_back(std::move(domain));
  }
  return {};
}
Status Location(const Json& value, bool chat, Json* location,
                ChatPlanError* error, const std::string& at) {
  if (value.IsNull()) return {};
  auto status = InputFields(
      value,
      chat ? std::initializer_list<std::string_view>{"type", "approximate"}
           : std::initializer_list<std::string_view>{"type", "city", "country",
                                                     "region", "timezone"},
      &error->param, at);
  if (!status) return status;
  if ((chat || value.Has("type")) && value.GetString("type") != "approximate")
    return Invalid(error, at + ".type", "Expected approximate location");
  const auto* fields = chat ? value.Find("approximate") : &value;
  if (!fields || !fields->IsObject())
    return Invalid(error, at + ".approximate", "Expected location object");
  const auto prefix = at + (chat ? ".approximate" : "");
  if (chat) {
    status = InputFields(*fields, {"city", "country", "region", "timezone"},
                         &error->param, prefix);
    if (!status) return status;
  }
  *location = Object({{"type", String("approximate")}});
  for (const auto* name : {"city", "country", "region", "timezone"}) {
    const auto* field = fields->Find(name);
    if (!field || (!chat && field->IsNull())) continue;
    if (!field->IsString() || field->str.size() > 256 ||
        field->str.find_first_of("\r\n\0", 0, 3) != field->str.npos)
      return Invalid(error, prefix + '.' + name, "Invalid location string");
    if (std::string_view(name) == "country" &&
        (field->str.size() != 2 ||
         !std::all_of(field->str.begin(), field->str.end(),
                      [](char c) { return c >= 'A' && c <= 'Z'; })))
      return Invalid(error, prefix + ".country", "Expected ISO country code");
    Set(location, name, *field);
  }
  return {};
}
}  // namespace

bool IsWebSearchType(std::string_view type) {
  return type == "web_search" || type == "web_search_2025_08_26" ||
         type == "web_search_preview" ||
         type == "web_search_preview_2025_03_11";
}
bool WebSearchDomainMatches(std::string_view host, std::string_view domain) {
  if (host.ends_with('.')) host.remove_suffix(1);
  return host == domain ||
         (host.size() > domain.size() && host.ends_with(domain) &&
          host[host.size() - domain.size() - 1] == '.');
}
bool WebSearchUrlAllowed(std::string_view raw,
                         const WebSearchOptions& options) {
  file_detail::Url url;
  if (!file_detail::ParseUrl(raw, &url)) return false;
  const auto matches = [&](const auto& domain) {
    return WebSearchDomainMatches(url.host, domain);
  };
  return (options.allowed_domains.empty() ||
          std::any_of(options.allowed_domains.begin(),
                      options.allowed_domains.end(), matches)) &&
         std::none_of(options.blocked_domains.begin(),
                      options.blocked_domains.end(), matches);
}

Status ParseWebSearchOptions(const Json& value, bool chat,
                             WebSearchOptions* output, ChatPlanError* error,
                             std::string_view path) {
  *output = {};
  *error = {};
  auto status = InputFields(
      value,
      chat ? std::initializer_list<std::string_view>{"search_context_size",
                                                     "user_location"}
           : std::initializer_list<std::string_view>{"type",
                                                     "search_context_size",
                                                     "user_location", "filters",
                                                     "external_web_access",
                                                     "return_token_budget",
                                                     "search_content_types",
                                                     "image_settings"},
      &error->param, path);
  if (!status) return status;
  const auto at = std::string(path);
  WebSearchOptions next;
  if (!chat) {
    next.type = value.GetString("type");
    if (!IsWebSearchType(next.type))
      return Invalid(error, at + ".type", "Expected web search type");
  }
  const bool preview = next.type.starts_with("web_search_preview");
  if (const auto* size = value.Find("search_context_size")) {
    if (!size->IsString() ||
        (size->str != "low" && size->str != "medium" && size->str != "high"))
      return Invalid(error, at + ".search_context_size",
                     "Expected low, medium or high");
    next.context_size = size->str;
  }
  if (const auto* location = value.Find("user_location")) {
    status =
        Location(*location, chat, &next.location, error, at + ".user_location");
    if (!status) return status;
  }
  if (const auto* filters = value.Find("filters")) {
    if (preview)
      return Invalid(error, at + ".filters",
                     "Domain filters require web_search");
    if (!filters->IsNull()) {
      status = InputFields(*filters, {"allowed_domains", "blocked_domains"},
                           &error->param, at + ".filters");
      if (!status) return status;
      for (const auto* name : {"allowed_domains", "blocked_domains"}) {
        const auto* domains = filters->Find(name);
        if (!domains) continue;
        auto* target = std::string_view(name) == "allowed_domains"
                           ? &next.allowed_domains
                           : &next.blocked_domains;
        status = Domains(*domains, at + ".filters." + name, target, error);
        if (!status) return status;
      }
    }
  }
  if (const auto* live = value.Find("external_web_access")) {
    if (!live->IsBool())
      return Invalid(error, at + ".external_web_access", "Expected boolean");
    // Official legacy contract always performs live access.
    if (!preview) next.external_web_access = live->boolean;
  }
  if (const auto* budget = value.Find("return_token_budget")) {
    if (preview || !budget->IsString() ||
        (budget->str != "default" && budget->str != "unlimited"))
      return Invalid(error, at + ".return_token_budget",
                     "Expected default or unlimited on web_search");
    next.unlimited_return_tokens = budget->str == "unlimited";
  }
  if (const auto* types = value.Find("search_content_types")) {
    if (preview || !types->IsArray() || types->array.empty() ||
        types->array.size() > 2)
      return Invalid(error, at + ".search_content_types",
                     "Expected text and/or image result types");
    next.text = false;
    for (const auto& type : types->array) {
      if (!type.IsString() || (type.str != "text" && type.str != "image") ||
          (type.str == "text" ? next.text : next.images))
        return Invalid(error, at + ".search_content_types",
                       "Expected distinct text/image result types");
      (type.str == "text" ? next.text : next.images) = true;
    }
  }
  if (const auto* images = value.Find("image_settings")) {
    if (preview || !next.images)
      return Invalid(error, at + ".image_settings",
                     "Image settings require image search");
    status = InputFields(*images, {"max_results", "caption"}, &error->param,
                         at + ".image_settings");
    if (!status) return status;
    if (const auto* count = images->Find("max_results")) {
      if (!IsInputInteger(*count) || count->number < 1 || count->number > 128)
        return Invalid(error, at + ".image_settings.max_results",
                       "Image result count exceeds request capacity (1..128)");
      next.max_images = count->number;
    }
    if (const auto* caption = images->Find("caption")) {
      if (!caption->IsBool())
        return Invalid(error, at + ".image_settings.caption",
                       "Expected boolean");
      next.captions = caption->boolean;
    }
  }
  *output = std::move(next);
  return {};
}
}  // namespace q4t::server
