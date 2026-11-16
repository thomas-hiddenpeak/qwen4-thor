#include "mcp_provider_graph.h"

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::mcp_detail::graph_detail {
using namespace object_detail;
using namespace provider_detail;
Json TeamsCatalog() {
  return Array({Tool("get_profile",
                     "Read the Graph profile; independently needs User.Read",
                     Object(), Array()),
                Tool("search",
                     "Search accessible Teams messages using Graph full-text "
                     "search; preserve original IDs, location and page offset",
                     SearchSchema(), Array({String("query")})),
                Tool("fetch",
                     "Read one Teams chat or channel message. Supply chat_id, "
                     "or both team_id and channel_id. For a channel reply, "
                     "message_id is the parent and reply_id is the reply.",
                     Object({{"message_id", TextSchema(2048, false)},
                             {"chat_id", TextSchema(2048)},
                             {"team_id", TextSchema(2048)},
                             {"channel_id", TextSchema(2048)},
                             {"reply_id", TextSchema(2048)}}),
                     Array({String("message_id")})),
                Tool("get_chat_members",
                     "Read a chat's membership page, preserving "
                     "opaque IDs and tenant identities",
                     Object({{"chat_id", TextSchema(2048, false)},
                             {"next_page_token", TextSchema(16384)}}),
                     Array({String("chat_id")}))});
}
ObjectReply TeamsRead(std::string_view name, const Json& args,
                      const ProviderFetch& fetch, Json* result,
                      CursorContext* context) {
  if (name == "get_profile") return Profile(args, fetch, result);
  if (name == "search") return Search("chatMessage", args, fetch, result);
  std::string field;
  if (name == "get_chat_members") {
    if (!InputFields(args, {"chat_id", "next_page_token"}, &field) ||
        !Id(Text(args, "chat_id")) ||
        !OptionalText(args, "next_page_token", 16384))
      return Invalid();
    const auto path =
        "/chats/" + ProviderEscape(Text(args, "chat_id")) + "/members";
    std::string url = std::string(kGraph) + path;
    if (!Text(args, "next_page_token").empty()) {
      auto reply =
          Cursor(Text(args, "next_page_token"), path, &url, nullptr, context);
      if (!reply.ok()) return reply;
    }
    Json raw;
    auto reply = fetch({url}, &raw);
    return reply.ok() ? Page(raw, path, "members", 65536, result, context)
                      : reply;
  }
  if (name != "fetch" ||
      !InputFields(
          args, {"chat_id", "team_id", "channel_id", "message_id", "reply_id"},
          &field))
    return Invalid();
  for (const char* key :
       {"chat_id", "team_id", "channel_id", "message_id", "reply_id"})
    if (!OptionalText(args, key, 2048) ||
        (!Text(args, key).empty() && !Id(Text(args, key))))
      return Invalid();
  const auto chat = Text(args, "chat_id"), team = Text(args, "team_id"),
             channel = Text(args, "channel_id"),
             message = Text(args, "message_id"),
             reply_id = Text(args, "reply_id");
  if (message.empty() ||
      (chat.empty() ? team.empty() || channel.empty()
                    : !team.empty() || !channel.empty() || !reply_id.empty()))
    return Invalid();
  std::string path = chat.empty() ? "/teams/" + ProviderEscape(team) +
                                        "/channels/" + ProviderEscape(channel)
                                  : "/chats/" + ProviderEscape(chat);
  path += "/messages/" + ProviderEscape(message);
  if (!reply_id.empty()) path += "/replies/" + ProviderEscape(reply_id);
  auto reply = fetch({std::string(kGraph) + path}, result);
  if (!reply.ok()) return reply;
  if (result->GetString("id") != (reply_id.empty() ? message : reply_id))
    return Protocol();
  const auto* body = result->Find("body");
  if (!body || body->IsNull()) {
    const auto* deleted = result->Find("deletedDateTime");
    if (deleted && deleted->IsString() && !deleted->str.empty()) return {};
    return Error(403, "mcp_provider_access_denied",
                 "The provider did not authorize the message body");
  }
  const auto* content = body->Find("content");
  if (!body->IsObject() || !content || !content->IsString() ||
      (body->GetString("contentType") != "text" &&
       body->GetString("contentType") != "html"))
    return Protocol();
  return {};
}
}  // namespace q4t::server::mcp_detail::graph_detail
