#include "q4t/server/responses_execution.h"

#include <stdexcept>

#include "object_protocol.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
using namespace object_detail;
namespace {
bool IncludeResults(const ResponsesEnvelope& envelope) {
  const auto* includes = envelope.request.GetArray("include");
  if (includes)
    for (const auto& value : includes->array)
      if (value.str == "file_search_call.results") return true;
  return false;
}
void HideResult(Json* item) {
  if (item->GetString("type") == "file_search_call" && item->Has("results"))
    Set(item, "results", Json());
}
void HideResponse(Json* response) {
  if (response->GetArray("output"))
    for (auto& item : Field(*response, "output").array) HideResult(&item);
}
}  // namespace

Json ResponsesExecution::PublicResponse() const {
  auto response = output_.Snapshot();
  if (!IncludeResults(envelope_)) HideResponse(&response);
  return ResponsesPublicValue(std::move(response));
}

std::string ResponsesExecution::PublicWire(std::string_view wire) const {
  if (IncludeResults(envelope_) ||
      wire.find("\"file_search_call\"") == wire.npos)
    return std::string(wire);
  std::string result;
  while (!wire.empty()) {
    const auto line = wire.find("\r\n");
    const auto end = wire.find("\r\n\r\n");
    if (!wire.starts_with("event: ") || line == wire.npos || end == wire.npos ||
        end <= line + 8 || wire.substr(line + 2, 6) != "data: ")
      throw std::logic_error("Invalid native file search event framing");
    Json event;
    const auto status =
        io::ParseJson(std::string(wire.substr(line + 8, end - line - 8)),
                      &event, true, kSchemaJsonLimits);
    if (!status || event.GetString("type") != wire.substr(7, line - 7))
      throw std::logic_error("Invalid native file search event value");
    if (event.Has("item")) HideResult(&Field(event, "item"));
    if (event.Has("response")) HideResponse(&Field(event, "response"));
    result +=
        std::string(wire.substr(0, line + 8)) + OutputJson(event) + "\r\n\r\n";
    wire.remove_prefix(end + 4);
  }
  return result;
}
}  // namespace q4t::server
