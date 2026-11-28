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
  const bool legacy_mcp = detail::HttpResponseContext::LegacyMcpError();
  const bool named_arguments =
      detail::HttpResponseContext::LegacyFunctionArgumentName();
  const bool stream_text =
      detail::HttpResponseContext::LegacyStreamOutputText();
  const bool hide_files = !Includes(envelope_, "file_search_call.results");
  const bool hide_sources =
      !Includes(envelope_, "web_search_call.action.sources");
  const auto project = [&](std::string_view frames) {
    const auto has = [&](std::string_view key) {
      return frames.find(key) != frames.npos;
    };
    return (legacy_mcp && has("\"mcp_call\"")) ||
           (named_arguments &&
            has("event: response.function_call_arguments.done\r\n")) ||
           (stream_text && (has("event: response.completed\r\n") ||
                            has("event: response.incomplete\r\n") ||
                            has("event: response.failed\r\n"))) ||
           (hide_files && has("\"file_search_call\"")) ||
           (hide_sources && has("\"web_search_call\""));
  };
  if (!project(wire)) return std::string(wire);
  std::string result;
  while (!wire.empty()) {
    const auto line = wire.find("\r\n");
    const auto end = wire.find("\r\n\r\n");
    if (!wire.starts_with("event: ") || line == wire.npos || end == wire.npos ||
        end <= line + 8 || wire.substr(line + 2, 6) != "data: ")
      throw std::logic_error("Invalid native Responses event framing");
    const auto frame = wire.substr(0, end + 4);
    if (!project(frame)) {
      result.append(frame);
      wire.remove_prefix(frame.size());
      continue;
    }
    // This is an already serialized native output frame. A legal completion
    // with many logprobs can exceed the input-schema node count. The exact
    // frame length bounds both the number of JSON values and decoded bytes.
    auto limits = kSchemaJsonLimits;
    limits.max_values = frame.size();
    limits.max_string_bytes = frame.size();
    Json event;
    const auto status =
        io::ParseJson(std::string(wire.substr(line + 8, end - line - 8)),
                      &event, true, limits);
    if (!status || event.GetString("type") != wire.substr(7, line - 7))
      throw std::logic_error("Invalid native Responses event value");
    if (event.Has("item")) HideResult(&Field(event, "item"), envelope_);
    if (event.Has("response"))
      HideResponse(&Field(event, "response"), envelope_);
    if (legacy_mcp) ProjectMcpErrorStrings(event);
    if (stream_text) ProjectStreamOutputText(&event);
    if (named_arguments &&
        !ProjectFunctionArgumentName(&event, output_.Snapshot()))
      throw std::logic_error("Function event has no matching output item");
    result +=
        std::string(wire.substr(0, line + 8)) + OutputJson(event) + "\r\n\r\n";
    wire.remove_prefix(end + 4);
  }
  return result;
}
}  // namespace q4t::server
