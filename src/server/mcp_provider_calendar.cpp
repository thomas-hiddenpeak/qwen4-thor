#include "mcp_provider_internal.h"

#include <algorithm>
#include <set>
#include <sstream>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
constexpr std::string_view kCalendar =
    "https://www.googleapis.com/calendar/v3/calendars/";
ObjectReply Invalid() {
  return Error(400, "mcp_provider_arguments_invalid",
               "Invalid native Calendar tool arguments");
}
ObjectReply Protocol() {
  return Error(502, "mcp_provider_response_invalid",
               "Invalid Calendar provider response");
}
Json TextSchema(size_t maximum, bool nullable = true) {
  auto type =
      nullable ? Array({String("string"), String("null")}) : String("string");
  return Object({{"type", std::move(type)},
                 {"minLength", Number(1)},
                 {"maxLength", Number(maximum)}});
}
Json Tool(std::string name, std::string description, Json properties,
          Json required = Array()) {
  return Object(
      {{"name", String(name)},
       {"description", String(description)},
       {"inputSchema", Object({{"type", String("object")},
                               {"properties", std::move(properties)},
                               {"required", std::move(required)},
                               {"additionalProperties", Boolean(false)}})},
       {"annotations", Object({{"readOnlyHint", Boolean(true)},
                               {"destructiveHint", Boolean(false)}})}});
}
bool OptionalText(const Json& args, const char* name, size_t limit) {
  const auto* value = args.Find(name);
  if (!value || value->IsNull()) return true;
  return value->IsString() && !value->str.empty() &&
         value->str.size() <= limit &&
         std::none_of(value->str.begin(), value->str.end(),
                      [](unsigned char c) { return c < 0x20 || c == 0x7f; });
}
std::string Text(const Json& args, const char* name,
                 std::string_view fallback = {}) {
  const auto* value = args.Find(name);
  return value && value->IsString() ? value->str : std::string(fallback);
}
bool Scope(const std::set<std::string>& scopes, std::string_view name) {
  return scopes.contains("https://www.googleapis.com/auth/" +
                         std::string(name));
}
ObjectReply Catalog(const ProviderFetch& fetch, Json* result) {
  Json token;
  auto reply = fetch({"https://oauth2.googleapis.com/tokeninfo", true}, &token);
  if (!reply.ok()) return reply;
  const auto* scope = token.Find("scope");
  if (!scope || !scope->IsString() || scope->str.size() > 16384)
    return Protocol();
  std::istringstream stream(scope->str);
  std::set<std::string> scopes;
  for (std::string name; stream >> name;) scopes.insert(name);
  auto tools = Array();
  if (Scope(scopes, "userinfo.email") || Scope(scopes, "userinfo.profile") ||
      scopes.contains("openid"))
    tools.array.push_back(
        Tool("get_profile", "Read the authorized user profile", Object()));
  bool calendar = false;
  for (const auto name :
       {"calendar", "calendar.readonly", "calendar.events",
        "calendar.events.readonly", "calendar.app.created",
        "calendar.events.owned", "calendar.events.owned.readonly",
        "calendar.events.public.readonly"})
    calendar = calendar || Scope(scopes, name);
  if (calendar) {
    auto search =
        Object({{"query", TextSchema(4096)},
                {"time_min", TextSchema(128)},
                {"time_max", TextSchema(128)},
                {"timezone_str", TextSchema(128)},
                {"calendar_id", TextSchema(1024)},
                {"next_page_token", TextSchema(8192)},
                {"max_results",
                 Object({{"type", Array({String("integer"), String("null")})},
                         {"minimum", Number(1)},
                         {"maximum", Number(2500)}})}});
    for (const auto* name : {"search", "search_events"})
      tools.array.push_back(
          Tool(name, "Search events; return one page and cursor", search));
    for (const auto* name : {"fetch", "read_event"})
      tools.array.push_back(
          Tool(name, "Read an event by its original Calendar event ID",
               Object({{"event_id", TextSchema(1024, false)},
                       {"calendar_id", TextSchema(1024)},
                       {"timezone_str", TextSchema(128)}}),
               Array({String("event_id")})));
  }
  *result = Object({{"tools", std::move(tools)}});
  return {};
}
Json Event(const Json& raw) {
  auto event = raw;
  for (const auto* key : {"start", "end"}) {
    if (const auto* value = raw.Find(key); value && value->IsObject()) {
      Set(&event, std::string(key) + "_details", *value);
      const auto* date = value->Find("dateTime");
      if (!date) date = value->Find("date");
      Set(&event, key, date ? *date : Json());
    }
  }
  const auto* url = raw.Find("htmlLink");
  Set(&event, "url", url ? *url : Json());
  Set(&event, "display_url", url ? *url : Json());
  const auto* title = raw.Find("summary");
  Set(&event, "display_title", title ? *title : Json());
  return event;
}
ObjectReply Read(std::string_view name, const Json& args,
                 const ProviderFetch& fetch, Json* value) {
  std::string field;
  if (name == "get_profile") {
    if (!InputFields(args, {}, &field)) return Invalid();
    return fetch({"https://openidconnect.googleapis.com/v1/userinfo"}, value);
  }
  const bool search = name == "search" || name == "search_events";
  if (!search && name != "fetch" && name != "read_event") return Invalid();
  if (!(search ? InputFields(args,
                             {"query", "time_min", "time_max", "timezone_str",
                              "calendar_id", "next_page_token", "max_results"},
                             &field)
               : InputFields(args, {"event_id", "calendar_id", "timezone_str"},
                             &field)))
    return Invalid();
  for (const auto& [key, maximum] : {std::pair{"query", 4096U},
                                     {"time_min", 128U},
                                     {"time_max", 128U},
                                     {"timezone_str", 128U},
                                     {"calendar_id", 1024U},
                                     {"next_page_token", 8192U},
                                     {"event_id", 1024U}})
    if (!OptionalText(args, key, maximum)) return Invalid();
  const auto calendar = Text(args, "calendar_id", "primary");
  if (calendar == "." || calendar == "..") return Invalid();
  std::string url = std::string(kCalendar) + ProviderEscape(calendar);
  std::vector<std::pair<std::string, std::string>> query;
  const auto timezone = Text(args, "timezone_str");
  if (!timezone.empty()) query.emplace_back("timeZone", timezone);
  int64_t maximum = 50;
  if (search) {
    const auto* max_results = args.Find("max_results");
    if (max_results && !max_results->IsNull()) {
      if (!IsInputInteger(*max_results) ||
          (maximum = max_results->AsInt()) < 1 || maximum > 2500)
        return Invalid();
    }
    query.emplace_back("maxResults", std::to_string(maximum));
    query.emplace_back("singleEvents", "true");
    query.emplace_back("orderBy", "startTime");
    if (!Text(args, "query").empty())
      query.emplace_back("q", Text(args, "query"));
    if (!Text(args, "next_page_token").empty())
      query.emplace_back("pageToken", Text(args, "next_page_token"));
    // Validate both bounds before even a timezone metadata request. A valid
    // first bound must not cause I/O when the second bound is malformed.
    for (const auto* input : {"time_min", "time_max"}) {
      const auto raw = Text(args, input);
      if (raw.empty()) continue;
      std::string ignored;
      const auto checked = ProviderCalendarTime(raw, timezone, &ignored);
      if (!checked.ok() && checked.code != "mcp_provider_timezone_required")
        return checked;
    }
    std::string zone = timezone;
    for (const auto& [input, output] :
         {std::pair{"time_min", "timeMin"}, {"time_max", "timeMax"}}) {
      const auto raw = Text(args, input);
      if (raw.empty()) continue;
      std::string utc;
      auto reply = ProviderCalendarTime(raw, zone, &utc);
      if (reply.code == "mcp_provider_timezone_required") {
        Json metadata;
        // The Events permission alone need not allow Calendars.get. A small
        // partial events listing provides the same calendar timezone without
        // requiring an unrelated OAuth scope or exposing an extra event.
        reply =
            fetch({url + "/events?maxResults=1&fields=timeZone"}, &metadata);
        if (!reply.ok()) return reply;
        const auto* tz = metadata.Find("timeZone");
        if (!tz || !tz->IsString() || tz->str.empty() || tz->str.size() > 128)
          return Protocol();
        zone = tz->str;
        reply = ProviderCalendarTime(raw, zone, &utc);
      }
      if (!reply.ok()) return reply;
      query.emplace_back(output, std::move(utc));
    }
    url += "/events";
  } else {
    const auto event = Text(args, "event_id");
    if (event.empty() || event == "." || event == "..") return Invalid();
    url += "/events/" + ProviderEscape(event);
  }
  for (const auto& [key, item] : query) {
    url += url.find('?') == url.npos ? '?' : '&';
    url += key + '=' + ProviderEscape(item);
  }
  Json response;
  auto reply = fetch({url}, &response);
  if (!reply.ok()) return reply;
  if (!response.IsObject()) return Protocol();
  if (search) {
    auto events = Array();
    if (const auto* items = response.Find("items")) {
      if (!items->IsArray() ||
          items->array.size() > static_cast<size_t>(maximum))
        return Protocol();
      for (const auto& item : items->array) {
        if (!item.IsObject() || item.GetString("id").empty()) return Protocol();
        events.array.push_back(Event(item));
      }
    }
    const auto* token = response.Find("nextPageToken");
    if (token &&
        (!token->IsString() || token->str.empty() || token->str.size() > 8192))
      return Protocol();
    *value = Object({{"events", std::move(events)},
                     {"next_page_token", token ? *token : Json()}});
  } else {
    if (response.GetString("id").empty()) return Protocol();
    *value = Event(response);
  }
  return {};
}
}  // namespace
ObjectReply GoogleCalendarProvider(std::string_view method, const Json& params,
                                   const ProviderFetch& fetch, Json* result) {
  if (method == "tools/list") {
    if (!params.IsObject() || !params.object.empty()) return Invalid();
    return Catalog(fetch, result);
  }
  const auto* args = params.Find("arguments");
  if (method != "tools/call" || !params.IsObject() || !args ||
      !args->IsObject())
    return Invalid();
  Json value;
  auto reply = Read(params.GetString("name"), *args, fetch, &value);
  if (!reply.ok()) return reply;
  *result = Object(
      {{"content", Array({Object({{"type", String("text")},
                                  {"text", String(OutputJson(value))}})})},
       {"structuredContent", std::move(value)},
       {"isError", Boolean(false)}});
  return {};
}
}  // namespace q4t::server::mcp_detail
