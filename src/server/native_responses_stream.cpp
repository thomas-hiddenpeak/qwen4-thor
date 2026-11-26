#include "q4t/server/native_responses_stream.h"

#include <stdexcept>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
bool Lifecycle(std::string_view type) {
  return type == "response.created" || type == "response.queued" ||
         type == "response.in_progress" || type == "response.completed" ||
         type == "response.incomplete" || type == "response.failed";
}
}  // namespace

NativeResponsesStream::NativeResponsesStream(Sink sink, size_t maximum_bytes,
                                             size_t event_bytes)
    : sink_(std::move(sink)),
      maximum_bytes_(maximum_bytes),
      event_bytes_(event_bytes) {
  if (!sink_ || !maximum_bytes_ || maximum_bytes_ > kChatBodyBytes ||
      !event_bytes_ || event_bytes_ > kChatBodyBytes)
    throw std::invalid_argument("Invalid native Responses stream owner");
}
bool NativeResponsesStream::Invalid(std::string_view message) {
  failed_ = true;
  if (error_.ok())
    error_ = object_detail::Error(503, "native_response_stream_invalid",
                                  std::string(message));
  return false;
}
bool NativeResponsesStream::Begin() {
  if (started_ || terminal_ || failed_ || sealed_)
    return Invalid("Native Responses stream already started or closed");
  started_ = true;
  return true;
}
bool NativeResponsesStream::Accept(Json event) {
  const auto type = event.GetString("type");
  const auto* number = event.Find("sequence_number");
  if (!event.IsObject() || !number || !IsInputInteger(*number) ||
      number->number != sequence_ ||
      (type != "error" && !type.starts_with("response.")) ||
      (id_.empty() && type != "response.created" && type != "error") ||
      (!id_.empty() && type == "response.created"))
    return Invalid("Native Responses event sequence or owner is invalid");
  const bool terminal = type == "error" || type == "response.completed" ||
                        type == "response.incomplete" ||
                        type == "response.failed";
  const auto* response = event.Find("response");
  if (Lifecycle(type)) {
    if (!response || !response->IsObject() ||
        !ValidApplicationId(response->GetString("id")) ||
        response->GetString("object") != "response" ||
        !response->GetArray("output") ||
        (!id_.empty() && response->GetString("id") != id_))
      return Invalid("Native Responses lifecycle lost its response identity");
    const auto status = response->GetString("status");
    if (type == "response.created"
            ? status != "in_progress" && status != "queued"
            : status != type.substr(9))
      return Invalid("Native Responses lifecycle status differs from event");
  }
  if (type == "error" &&
      (event.GetString("code").empty() || !event.Find("message") ||
       !event.Find("message")->IsString()))
    return Invalid("Native Responses error has no actual failure");
  // Save the terminal before invoking the sink, which may have observed it
  // even if its receipt is lost. Failed acceptance still poisons this owner.
  if (terminal && response) response_ = *response;
  auto reply = sink_(event);
  if (!Accepted(reply)) {
    if (reply.code.empty()) {
      reply.code = "native_response_sink_unconfirmed";
      reply.message = "Native Responses sink did not confirm acceptance";
      reply.http_status = 503;
    }
    error_ = std::move(reply);
    failed_ = true;
    return false;
  }
  if (type == "response.created") id_ = response->GetString("id");
  if (type == "error")
    error_ = object_detail::Error(500, event.GetString("code"),
                                  event.GetString("message"));
  if (type == "response.failed") {
    const auto* cause = response->Find("error");
    // Internally routed turns retain the original diagnostic carried by our
    // own Responses producer, including streams written by older releases.
    auto code = cause ? cause->GetString("type") : "";
    if (code.empty() && cause) code = cause->GetString("code");
    error_ = object_detail::Error(
        500, code.empty() ? "response_failed" : std::move(code),
        cause ? cause->GetString("message", "Response failed")
              : "Response failed");
  }
  ++sequence_;
  terminal_ = terminal;
  return true;
}
bool NativeResponsesStream::Append(std::string_view wire) {
  if (!started_ || failed_ || sealed_ || (terminal_ && !wire.empty()))
    return Invalid("Native Responses stream is not writable");
  if (wire.size() > maximum_bytes_ - bytes_)
    return Invalid("Native Responses stream byte capacity exhausted");
  try {
    while (!wire.empty()) {
      const auto line = wire.find("\r\n");
      const auto end = wire.find("\r\n\r\n");
      if (terminal_ || !wire.starts_with("event: ") || line == wire.npos ||
          end == wire.npos || line < 8 || end <= line + 8 ||
          wire.substr(line + 2, 6) != "data: " || end + 4 > event_bytes_)
        return Invalid("Invalid native Responses event framing or capacity");
      Json event;
      const auto status =
          io::ParseJson(std::string(wire.substr(line + 8, end - line - 8)),
                        &event, true, kApplicationJsonLimits);
      if (!status || event.GetString("type") != wire.substr(7, line - 7))
        return Invalid("Native Responses event payload differs from frame");
      if (!Accept(std::move(event))) return false;
      bytes_ += end + 4;
      wire.remove_prefix(end + 4);
    }
    return true;
  } catch (...) {
    failed_ = true;
    throw;
  }
}
bool NativeResponsesStream::RecordError(int status,
                                        std::string_view content_type,
                                        std::string_view body) {
  if (started_ || terminal_ || failed_ || sealed_ || status < 400 ||
      status > 599 || content_type != "application/json" ||
      body.size() > maximum_bytes_ || body.size() > event_bytes_)
    return Invalid("Invalid native Responses finite error");
  try {
    Json value;
    if (!io::ParseJson(std::string(body), &value, true, kApplicationJsonLimits))
      return Invalid("Malformed native Responses finite error");
    const auto* error = value.Find("error");
    if (!error || !error->IsObject() || error->GetString("code").empty() ||
        !error->Find("message") || !error->Find("message")->IsString())
      return Invalid("Missing native Responses finite failure");
    error_ = object_detail::Error(status, error->GetString("code"),
                                  error->GetString("message"));
    error_.value = std::move(value);
    bytes_ = body.size();
    terminal_ = true;
    return true;
  } catch (...) {
    failed_ = true;
    throw;
  }
}
void NativeResponsesStream::Seal() noexcept {
  sealed_ = true;
  if (!terminal_) failed_ = true;
}
}  // namespace q4t::server
