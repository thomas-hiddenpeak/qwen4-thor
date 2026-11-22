#include "q4t/server/live_media_policy.h"

#include <algorithm>

namespace q4t::server {
namespace {
constexpr size_t kMaxSelectors = 256;

Status Invalid(ChatPlanError* error, const std::string& field) {
  *error = {"invalid_request", "session.client.data_channel" + field, 400};
  return Status::Fail("Invalid Live data channel event permission");
}

bool All(const io::Json* field) {
  return !field || (field->IsString() && field->str == "all");
}

bool ValidSelector(std::string_view text) {
  // The wire parser has already validated UTF-8. Schema length is measured
  // in Unicode scalar values, including for an unknown future event type.
  return !text.empty() && text.size() <= 4 * 256 &&
         std::ranges::count_if(
             text, [](unsigned char c) { return (c & 0xc0) != 0x80; }) <= 256;
}
}  // namespace

Status LiveMediaPolicy::Configure(const io::Json* client,
                                  ChatPlanError* error) {
  if (!error) return Status::Fail("Missing Live data channel error owner");
  *error = {};
  LiveMediaPolicy next;
  if (client) {
    const auto* config = client->Find("data_channel");
    if (!client->IsObject() || client->object.size() != 1 || !config ||
        !config->IsObject())
      return Invalid(error, "");
    for (const auto& [key, _] : config->object)
      if (key != "allowed_client_events" && key != "allowed_server_events")
        return Invalid(error, "");
    const auto* allowed = config->Find("allowed_client_events");
    if (!All(allowed)) {
      if (!allowed->IsArray() || allowed->array.size() > kMaxSelectors)
        return Invalid(error, ".allowed_client_events");
      next.client_.emplace();
      for (const auto& entry : allowed->array) {
        if (!entry.IsString()) return Invalid(error, ".allowed_client_events");
        next.client_->insert(entry.str);
      }
    }
    const auto* selectors = config->Find("allowed_server_events");
    if (!All(selectors)) {
      if (!selectors->IsArray() || selectors->array.size() > kMaxSelectors)
        return Invalid(error, ".allowed_server_events");
      next.server_.emplace();
      for (const auto& entry : selectors->array) {
        const auto type = entry.GetString("type");
        const auto* inner = entry.Find("response_event");
        const bool nested = type == "response.event";
        if (!ValidSelector(type) || entry.object.size() != (nested ? 2u : 1u) ||
            (nested &&
             (!inner || !inner->IsString() || !ValidSelector(inner->str))) ||
            (!nested && inner))
          return Invalid(error, ".allowed_server_events");
        next.server_->emplace(type, inner ? inner->str : "");
      }
    }
  }
  *this = std::move(next);
  return {};
}

bool LiveMediaPolicy::AllowsClient(std::string_view type) const {
  // The HTTP owner starts the session. Audio comes from the negotiated track.
  return !type.empty() && type != "session.start" &&
         type != "session.input_audio.append" &&
         (!client_ || client_->contains(type));
}

bool LiveMediaPolicy::AllowsServer(const io::Json& event) const {
  auto type = event.GetString("type");
  if (type.empty() || type == "session.output_audio.delta" ||
      type == "session.input_audio.append")
    return false;
  std::string nested;
  if (type == "response.event") {
    const auto* inner = event.Find("event");
    if (!inner || (nested = inner->GetString("type")).empty()) return false;
  }
  return !server_ || server_->contains({std::move(type), std::move(nested)});
}
}  // namespace q4t::server
