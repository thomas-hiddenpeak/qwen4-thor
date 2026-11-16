#include "mcp_provider_graph.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::mcp_detail::graph_detail {
namespace {
using namespace object_detail;
using namespace provider_detail;
constexpr std::string_view kMailFields =
    "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "sentDateTime,bodyPreview,isRead,hasAttachments,webLink,"
    "conversationId,importance";
void MailMetadata(Json* value) {
  for (auto& [key, items] : value->object) {
    if (key != "value" || !items.IsArray()) continue;
    for (auto& item : items.array) {
      std::erase_if(item.object, [](const auto& field) {
        // Pagination URLs cannot widen a list-only tool into full-message
        // access, even when an upstream continuation omits $select.
        const std::string fields = "," + std::string(kMailFields) + ",";
        return field.first != "@odata.type" && field.first != "@odata.etag" &&
               fields.find("," + field.first + ",") == fields.npos;
      });
    }
  }
}
Json IdsSchema() {
  return Object({{"type", String("array")},
                 {"minItems", Number(1)},
                 {"maxItems", Number(20)},
                 {"items", TextSchema(2048, false)}});
}
bool Zone(std::string_view zone) {
  return zone.size() <= 128 &&
         std::all_of(zone.begin(), zone.end(), [](unsigned char c) {
           return c >= 32 && c < 127 && c != '"' && c != '\\';
         });
}
std::string CalendarPath(const Json& args) {
  const auto id = Text(args, "calendar_id");
  return id.empty() ? "/me/calendar" : "/me/calendars/" + ProviderEscape(id);
}
ProviderRequest Get(std::string url, bool calendar,
                    std::string_view timezone = {}) {
  ProviderRequest request{std::move(url)};
  request.headers["Prefer"] =
      calendar ? "outlook.timezone=\"" +
                     std::string(timezone.empty() ? "UTC" : timezone) + "\""
               : "outlook.body-content-type=\"text\"";
  return request;
}
ObjectReply One(bool calendar, std::string_view path, std::string_view id,
                std::string_view zone, const ProviderFetch& fetch,
                Json* result) {
  auto reply = fetch(
      Get(std::string(kGraph) + std::string(path) + "/" + ProviderEscape(id),
          calendar, zone),
      result);
  if (!reply.ok()) return reply;
  if (!result->IsObject() || result->GetString("id") != id) return Protocol();
  if (!calendar) {
    const auto* body = result->Find("body");
    // Mail.ReadBasic can expose metadata without the body. A fetch must not
    // quietly turn that permission restriction into an empty full message.
    if (!body || body->IsNull())
      return Error(403, "mcp_provider_access_denied",
                   "The provider did not authorize the message body");
    const auto* text = body->Find("content");
    if (!body->IsObject() || !text || !text->IsString() ||
        (body->GetString("contentType") != "text" &&
         body->GetString("contentType") != "html"))
      return Protocol();
  }
  return {};
}
ObjectReply Fetch(bool calendar, bool batch, const Json& args,
                  const ProviderFetch& fetch, Json* result) {
  std::string field;
  const char* id = calendar ? "event_id" : "message_id";
  const char* ids = calendar ? "event_ids" : "message_ids";
  if (!(calendar
            ? batch ? InputFields(args,
                                  {"event_ids", "calendar_id", "timezone_str"},
                                  &field)
                    : InputFields(args,
                                  {"event_id", "calendar_id", "timezone_str"},
                                  &field)
        : batch ? InputFields(args, {"message_ids"}, &field)
                : InputFields(args, {"message_id"}, &field)))
    return Invalid();
  if (!OptionalText(args, "calendar_id", 2048) ||
      !OptionalText(args, "timezone_str", 128) ||
      (!Text(args, "calendar_id").empty() && !Id(Text(args, "calendar_id"))) ||
      !Zone(Text(args, "timezone_str")))
    return Invalid();
  std::vector<std::string> requested;
  if (batch) {
    const auto* values = args.GetArray(ids);
    if (!values || values->array.empty() || values->array.size() > 20)
      return Invalid();
    for (const auto& item : values->array) {
      if (!item.IsString() || !Id(item.str)) return Invalid();
      requested.push_back(item.str);
    }
  } else {
    if (!OptionalText(args, id, 2048) || !Id(Text(args, id))) return Invalid();
    requested.push_back(Text(args, id));
  }
  const auto path = calendar ? CalendarPath(args) + "/events" : "/me/messages";
  auto items = Array();
  for (const auto& item : requested) {
    Json value;
    auto reply =
        One(calendar, path, item, Text(args, "timezone_str"), fetch, &value);
    if (!reply.ok()) return reply;
    items.array.push_back(std::move(value));
  }
  *result = batch
                ? Object({{calendar ? "events" : "messages", std::move(items)}})
                : std::move(items.array[0]);
  return {};
}
std::string TimeKey(std::string value) {
  value.pop_back();  // ProviderCalendarTime emits UTC Z.
  const auto dot = value.find('.');
  if (dot == value.npos)
    value += ".000000000";
  else if (value.size() - dot < 10)
    value.append(10 - (value.size() - dot), '0');
  return value;
}
ObjectReply List(bool calendar, std::string_view name, const Json& args,
                 const ProviderFetch& fetch, Json* result) {
  std::string field;
  const bool search = name == "search_messages";
  if (!(calendar
            ? InputFields(args,
                          {"calendar_id", "time_min", "time_max",
                           "timezone_str", "max_results", "next_page_token"},
                          &field)
        : search
            ? InputFields(
                  args,
                  {"query", "folder_id", "max_results", "next_page_token"},
                  &field)
            : InputFields(args, {"folder_id", "max_results", "next_page_token"},
                          &field)))
    return Invalid();
  int maximum = 0;
  if (!Limit(args, 25, 1000, &maximum)) return Invalid();
  for (const auto& [key, limit] : {std::pair{"calendar_id", 2048U},
                                   {"folder_id", 2048U},
                                   {"time_min", 128U},
                                   {"time_max", 128U},
                                   {"timezone_str", 128U},
                                   {"next_page_token", 8192U},
                                   {"query", 4096U}})
    if (!OptionalText(args, key, limit)) return Invalid();
  for (const auto* key : {"calendar_id", "folder_id"})
    if (!Text(args, key).empty() && !Id(Text(args, key))) return Invalid();
  if (!Zone(Text(args, "timezone_str")) ||
      (search && Text(args, "query").empty()))
    return Invalid();
  const bool window =
      !Text(args, "time_min").empty() || !Text(args, "time_max").empty();
  std::string start, end;
  if (window) {
    if (Text(args, "time_min").empty() || Text(args, "time_max").empty())
      return Invalid();
    auto reply = ProviderCalendarTime(Text(args, "time_min"), "UTC", &start);
    if (reply.ok())
      reply = ProviderCalendarTime(Text(args, "time_max"), "UTC", &end);
    if (!reply.ok()) return reply;
    if (TimeKey(start) >= TimeKey(end)) return Invalid();
  }
  const auto folder = Text(args, "folder_id");
  const std::string path =
      calendar ? CalendarPath(args) + (window ? "/calendarView" : "/events")
      : folder.empty()
          ? "/me/messages"
          : "/me/mailFolders/" + ProviderEscape(folder) + "/messages";
  std::string url = std::string(kGraph) + path;
  const auto cursor = Text(args, "next_page_token");
  if (!cursor.empty()) {
    std::map<std::string, std::string> continuation;
    auto reply = Cursor(cursor, path, &url, &continuation);
    if (!reply.ok()) return reply;
    if (!calendar) {
      for (const auto& [key, value] : continuation) {
        if (key != "$skip" && key != "$skiptoken" && key != "$top" &&
            key != "$search" && key != "$select" && key != "$orderby")
          return Invalid();
        if ((key == "$select" && value != kMailFields) ||
            (key == "$search" && !search) ||
            (key == "$orderby" && (search || value != "receivedDateTime desc")))
          return Invalid();
      }
    }
    // The complete upstream URL owns page-size/query continuation. Don't
    // silently ignore a new limit or search term alongside the cursor.
    if (args.Has("max_results") && !args.Find("max_results")->IsNull() &&
        (!continuation.contains("$top") ||
         continuation.at("$top") != std::to_string(maximum)))
      return Invalid();
    if (search && !Text(args, "query").empty()) {
      // Search query is mandatory for that tool's schema. Verify it remains
      // represented by the original $search parameter below.
      std::string quoted = "\"";
      for (char c : Text(args, "query")) {
        if (c == '"' || c == '\\') quoted += '\\';
        quoted += c;
      }
      quoted += '"';
      if (!continuation.contains("$search") ||
          continuation.at("$search") != quoted)
        return Invalid();
    }
    if (window) {
      std::string original_start, original_end;
      if (!continuation.contains("startDateTime") ||
          !continuation.contains("endDateTime") ||
          !ProviderCalendarTime(continuation.at("startDateTime"), "UTC",
                                &original_start)
               .ok() ||
          !ProviderCalendarTime(continuation.at("endDateTime"), "UTC",
                                &original_end)
               .ok() ||
          TimeKey(original_start) != TimeKey(start) ||
          TimeKey(original_end) != TimeKey(end))
        return Invalid();
    }
  } else {
    Query(&url, "$top", std::to_string(maximum));
    if (calendar) {
      if (window) {
        Query(&url, "startDateTime", start);
        Query(&url, "endDateTime", end);
      }
    } else {
      Query(&url, "$select", kMailFields);
      if (search) {
        std::string quoted = "\"";
        for (char c : Text(args, "query")) {
          if (c == '"' || c == '\\') quoted += '\\';
          quoted += c;
        }
        Query(&url, "$search", quoted + '"');
      } else
        Query(&url, "$orderby", "receivedDateTime desc");
    }
  }
  Json response;
  auto reply = fetch(Get(url, calendar, Text(args, "timezone_str")), &response);
  if (reply.ok() && !calendar) MailMetadata(&response);
  return reply.ok() ? Page(response, path, calendar ? "events" : "messages",
                           cursor.empty() ? maximum : 1000, result)
                    : reply;
}
}  // namespace
io::Json OutlookCatalog(bool calendar) {
  using namespace object_detail;
  using namespace provider_detail;
  auto tools = Array({Tool("get_profile",
                           "Read the Graph user profile; requires User.Read "
                           "independently of mail/calendar permissions",
                           Object(), Array())});
  if (calendar) {
    tools.array.push_back(Tool("search_events",
                               "Search the signed-in user's primary calendar "
                               "using Graph full-text search; preserve hit "
                               "IDs, page totals and offset",
                               SearchSchema(), Array({String("query")})));
    auto props = Object(
        {{"calendar_id", TextSchema(2048)}, {"timezone_str", TextSchema(128)}});
    auto single = props;
    Set(&single, "event_id", TextSchema(2048, false));
    tools.array.push_back(Tool("fetch_event", "Read an original calendar event",
                               single, Array({String("event_id")})));
    auto batch = props;
    Set(&batch, "event_ids", IdsSchema());
    tools.array.push_back(Tool("fetch_events_batch",
                               "Read up to20 events in input order; "
                               "any failure fails the complete batch",
                               batch, Array({String("event_ids")})));
    Set(&props, "time_min", TextSchema(128));
    Set(&props, "time_max", TextSchema(128));
    Set(&props, "max_results", LimitSchema(1000));
    Set(&props, "next_page_token", TextSchema(8192));
    tools.array.push_back(Tool(
        "list_events",
        "List one page. Supply both time bounds for "
        "expanded recurring events; naive input times are UTC. timezone_str "
        "controls returned "
        "times. To continue, keep calendar/time bounds and omit max_results.",
        props, Array()));
  } else {
    for (const auto* name :
         {"list_messages", "search_messages", "get_recent_emails"}) {
      auto props = Object({{"folder_id", TextSchema(2048)},
                           {"max_results", LimitSchema(1000)},
                           {"next_page_token", TextSchema(8192)}});
      auto required = Array();
      if (std::string_view(name) == "search_messages") {
        Set(&props, "query", TextSchema(4096, false));
        required.array.push_back(String("query"));
      }
      tools.array.push_back(
          Tool(name,
               "Read one metadata page; searches use Outlook search "
               "syntax, other lists newest first. Continue with the same "
               "folder/query and omit "
               "max_results. fetch_message reads full body.",
               props, required));
    }
    tools.array.push_back(
        Tool("fetch_message", "Read the original full message and body",
             Object({{"message_id", TextSchema(2048, false)}}),
             Array({String("message_id")})));
    tools.array.push_back(Tool("fetch_messages_batch",
                               "Read up to20 full messages in input "
                               "order; any failure fails the complete batch",
                               Object({{"message_ids", IdsSchema()}}),
                               Array({String("message_ids")})));
  }
  return tools;
}
ObjectReply OutlookRead(bool calendar, std::string_view name,
                        const io::Json& args, const ProviderFetch& fetch,
                        io::Json* result) {
  using namespace provider_detail;
  if (name == "get_profile") return Profile(args, fetch, result);
  if (calendar && name == "search_events")
    return Search("event", args, fetch, result);
  if (name == (calendar ? "fetch_event" : "fetch_message"))
    return Fetch(calendar, false, args, fetch, result);
  if (name == (calendar ? "fetch_events_batch" : "fetch_messages_batch"))
    return Fetch(calendar, true, args, fetch, result);
  if (calendar ? name == "list_events"
               : name == "list_messages" || name == "search_messages" ||
                     name == "get_recent_emails")
    return List(calendar, name, args, fetch, result);
  return Invalid();
}
}  // namespace q4t::server::mcp_detail::graph_detail
