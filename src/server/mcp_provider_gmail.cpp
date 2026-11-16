#include "mcp_provider_mail.h"

#include <set>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
using namespace provider_detail;
constexpr std::string_view kGmail =
    "https://gmail.googleapis.com/gmail/v1/users/me/";
Json BoolSchema() {
  return Object({{"type", Array({String("boolean"), String("null")})}});
}
bool OptionalBool(const Json& args, const char* key) {
  const auto* value = args.Find(key);
  return !value || value->IsNull() || value->IsBool();
}
ObjectReply Catalog(const ProviderFetch& fetch, Json* result) {
  std::set<std::string> scopes;
  auto reply = GoogleScopes(fetch, &scopes);
  if (!reply.ok()) return reply;
  const bool read = scopes.contains("https://mail.google.com/") ||
                    Scope(scopes, "gmail.readonly") ||
                    Scope(scopes, "gmail.modify");
  auto tools = Array();
  if (read || Scope(scopes, "gmail.metadata"))
    tools.array.push_back(Tool(
        "get_profile", "Read the authorized Gmail profile", Object(), Array()));
  if (read) {
    for (const auto* name :
         {"search_emails", "search_email_ids", "get_recent_emails"}) {
      auto args = Object(
          {{"max_results",
            LimitSchema(std::string_view(name) == "search_email_ids" ? 500
                                                                     : 20)},
           {"next_page_token", TextSchema(8192)},
           {"include_spam_trash", BoolSchema()}});
      auto required = Array();
      if (std::string_view(name) != "get_recent_emails") {
        Set(&args, "query", TextSchema(4096, false));
        required.array.push_back(String("query"));
      }
      tools.array.push_back(Tool(
          name,
          "Read one page from Gmail; search uses Gmail query syntax. "
          "Expanded searches return metadata, read_email returns the body.",
          std::move(args), std::move(required)));
    }
    tools.array.push_back(Tool(
        "read_email",
        "Read full MIME bodies. Attachment "
        "metadata is preserved; include_attachments fetches bytes explicitly.",
        Object({{"message_id", TextSchema(1024, false)},
                {"include_attachments", BoolSchema()}}),
        Array({String("message_id")})));
    tools.array.push_back(Tool(
        "batch_read_email",
        "Read up to 20 full messages "
        "in original order; any failed read fails the batch without partial "
        "success.",
        Object({{"message_ids", Object({{"type", String("array")},
                                        {"minItems", Number(1)},
                                        {"maxItems", Number(20)},
                                        {"items", TextSchema(1024, false)}})},
                {"include_attachments", BoolSchema()}}),
        Array({String("message_ids")})));
  }
  *result = Object({{"tools", std::move(tools)}});
  return {};
}
ObjectReply ReadOne(std::string_view id, bool full, bool attachments,
                    const ProviderFetch& fetch, Json* value, Json* content) {
  std::string url = std::string(kGmail) + "messages/" + ProviderEscape(id);
  Query(&url, "format", full ? "full" : "metadata");
  Json response;
  auto reply = fetch({url}, &response);
  return reply.ok() ? GmailMessage(response, id, full, attachments, fetch,
                                   value, content)
                    : reply;
}
ObjectReply Read(std::string_view name, const Json& args,
                 const ProviderFetch& fetch, Json* value, Json* content) {
  std::string field;
  if (name == "get_profile") {
    if (!InputFields(args, {}, &field)) return Invalid();
    auto reply = fetch({std::string(kGmail) + "profile"}, value);
    if (!reply.ok()) return reply;
    return value->GetString("emailAddress").empty() ? Protocol()
                                                    : ObjectReply{};
  }
  if (name == "read_email" || name == "batch_read_email") {
    const bool batch = name == "batch_read_email";
    if (!(batch ? InputFields(args, {"message_ids", "include_attachments"},
                              &field)
                : InputFields(args, {"message_id", "include_attachments"},
                              &field)) ||
        !OptionalBool(args, "include_attachments"))
      return Invalid();
    std::vector<std::string> ids;
    if (batch) {
      const auto* values = args.GetArray("message_ids");
      if (!values || values->array.empty() || values->array.size() > 20)
        return Invalid();
      for (const auto& item : values->array) {
        if (!item.IsString() || !Identifier(item.str)) return Invalid();
        ids.push_back(item.str);
      }
    } else {
      if (!OptionalText(args, "message_id", 1024) ||
          !Identifier(Text(args, "message_id")))
        return Invalid();
      ids.push_back(Text(args, "message_id"));
    }
    auto messages = Array();
    for (const auto& id : ids) {
      Json message;
      auto reply = ReadOne(id, true, args.GetBool("include_attachments"), fetch,
                           &message, content);
      if (!reply.ok()) return reply;
      messages.array.push_back(std::move(message));
    }
    *value = batch ? Object({{"messages", std::move(messages)}})
                   : std::move(messages.array[0]);
    return {};
  }
  const bool ids_only = name == "search_email_ids";
  const bool recent = name == "get_recent_emails";
  if (!ids_only && !recent && name != "search_emails") return Invalid();
  if (!(recent ? InputFields(
                     args,
                     {"max_results", "next_page_token", "include_spam_trash"},
                     &field)
               : InputFields(args,
                             {"query", "max_results", "next_page_token",
                              "include_spam_trash"},
                             &field)))
    return Invalid();
  int maximum = 0;
  if (!Limit(args, ids_only ? 100 : 10, ids_only ? 500 : 20, &maximum) ||
      !OptionalText(args, "query", 4096) ||
      !OptionalText(args, "next_page_token", 8192) ||
      !OptionalBool(args, "include_spam_trash") ||
      (!recent && Text(args, "query").empty()))
    return Invalid();
  std::string url = std::string(kGmail) + "messages";
  Query(&url, "maxResults", std::to_string(maximum));
  if (!recent) Query(&url, "q", Text(args, "query"));
  if (!Text(args, "next_page_token").empty())
    Query(&url, "pageToken", Text(args, "next_page_token"));
  Query(&url, "includeSpamTrash",
        args.GetBool("include_spam_trash") ? "true" : "false");
  Json response;
  auto reply = fetch({url}, &response);
  if (!reply.ok()) return reply;
  reply = Page(response, "messages", maximum, value);
  if (!reply.ok()) return reply;
  const auto items = *value->GetArray("messages");
  for (const auto& item : items.array)
    if (!Identifier(item.GetString("id"))) return Protocol();
  auto messages = Array();
  for (const auto& item : items.array) {
    if (ids_only) {
      messages.array.push_back(item);
      continue;
    }
    Json message;
    reply =
        ReadOne(item.GetString("id"), false, false, fetch, &message, content);
    if (!reply.ok()) return reply;
    messages.array.push_back(std::move(message));
  }
  Set(value, "messages", std::move(messages));
  return {};
}
}  // namespace
ObjectReply GmailProvider(std::string_view method, const Json& params,
                          const ProviderFetch& fetch, Json* result) {
  if (method == "tools/list") {
    if (!params.IsObject() || !params.object.empty()) return Invalid();
    return Catalog(fetch, result);
  }
  const auto* args = params.Find("arguments");
  if (method != "tools/call" || !params.IsObject() || !args ||
      !args->IsObject())
    return Invalid();
  auto content = Array();
  Json value;
  auto reply = Read(params.GetString("name"), *args, fetch, &value, &content);
  return reply.ok() ? Result(std::move(value), std::move(content), result)
                    : reply;
}
}  // namespace q4t::server::mcp_detail
