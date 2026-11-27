#include "q4t/server/responses_execution.h"

#include <stdexcept>
#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
using namespace object_detail;
namespace {
bool Includes(const ResponsesEnvelope& envelope, std::string_view field) {
  const auto* includes = envelope.request.GetArray("include");
  if (includes)
    for (const auto& value : includes->array)
      if (value.str == field) return true;
  return false;
}
void HideResult(Json* item, const ResponsesEnvelope& envelope) {
  if (!Includes(envelope, "file_search_call.results") &&
      item->GetString("type") == "file_search_call" && item->Has("results"))
    Set(item, "results", Json());
  if (!Includes(envelope, "web_search_call.action.sources") &&
      item->GetString("type") == "web_search_call" && item->Has("action"))
    std::erase_if(Field(*item, "action").object,
                  [](const auto& field) { return field.first == "sources"; });
}
void HideResponse(Json* response, const ResponsesEnvelope& envelope) {
  if (response->GetArray("output"))
    for (auto& item : Field(*response, "output").array) HideResult(&item, envelope);
}
}  // namespace

Json ResponsesExecution::PublicResponse() const {
  auto response = output_.Snapshot();
  HideResponse(&response, envelope_);
  return ResponsesWireValue(std::move(response));
}

std::string ResponsesExecution::PublicWire(std::string_view wire) const {
  const bool legacy_mcp = detail::HttpResponseContext::LegacyMcpError() &&
                          wire.find("\"mcp_call\"") != wire.npos;
  if (!legacy_mcp && (Includes(envelope_, "file_search_call.results") ||
       wire.find("\"file_search_call\"") == wire.npos) &&
      (Includes(envelope_, "web_search_call.action.sources") ||
       wire.find("\"web_search_call\"") == wire.npos))
    return std::string(wire);
  std::string result;
  while (!wire.empty()) {
    const auto line = wire.find("\r\n");
    const auto end = wire.find("\r\n\r\n");
    if (!wire.starts_with("event: ") || line == wire.npos || end == wire.npos ||
        end <= line + 8 || wire.substr(line + 2, 6) != "data: ")
      throw std::logic_error("Invalid native Responses event framing");
    Json event;
    const auto status =
        io::ParseJson(std::string(wire.substr(line + 8, end - line - 8)),
                      &event, true, kSchemaJsonLimits);
    if (!status || event.GetString("type") != wire.substr(7, line - 7))
      throw std::logic_error("Invalid native Responses event value");
    if (event.Has("item")) HideResult(&Field(event, "item"), envelope_);
    if (event.Has("response")) HideResponse(&Field(event, "response"), envelope_);
    if (legacy_mcp) ProjectMcpErrorStrings(event);
    result +=
        std::string(wire.substr(0, line + 8)) + OutputJson(event) + "\r\n\r\n";
    wire.remove_prefix(end + 4);
  }
  return result;
}
}  // namespace q4t::server
