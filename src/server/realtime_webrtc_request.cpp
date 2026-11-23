#include "realtime_webrtc_request.h"

#include "object_protocol.h"
#include "q4t/server/multipart.h"
#include "q4t/server/realtime_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kSdpBytes = 65535;
std::string MediaType(std::string_view header) {
  auto type = header.substr(0, header.find(';'));
  const auto last = type.find_last_not_of(" \t");
  type = type.substr(0, last + 1);
  std::string result(type);
  for (auto& c : result)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return result;
}
}  // namespace

ObjectReply ParseRealtimeWebRtcOffer(std::string_view content_type,
                                     std::string_view body,
                                     RealtimeWebRtcOffer* offer) {
  RealtimeWebRtcOffer next;
  next.session = Object({{"type", String("realtime")}});
  std::string configuration;
  const auto media = MediaType(content_type);
  if (media == "application/sdp") {
    if (body.size() > kSdpBytes)
      return Error(400, "live_invalid_sdp",
                   "SDP offer exceeds the media limit");
    next.sdp = body;
  } else if (media == "multipart/form-data") {
    std::string boundary;
    auto status = MultipartBoundary(content_type, &boundary);
    if (!status) return Error(400, "invalid_request", status.message());
    std::string* field = nullptr;
    bool sdp = false, session = false;
    MultipartDecoder parts(
        std::move(boundary), body.size(), 2,
        {[&](const MultipartPart& part) {
           const auto type = MediaType(part.content_type);
           if (part.name == "sdp" && !sdp &&
               (type == "application/sdp" || type == "text/plain")) {
             field = &next.sdp;
             sdp = true;
           } else if (part.name == "session" && !session &&
                      (type == "application/json" || type == "text/plain")) {
             field = &configuration;
             session = true;
           } else {
             return Status::Fail("Unexpected or duplicate WebRTC form field");
           }
           return Status{};
         },
         [&](std::string_view bytes) {
           if (field == &next.sdp && bytes.size() > kSdpBytes - field->size())
             return Status::Fail("SDP offer exceeds the media limit");
           field->append(bytes);
           return Status{};
         },
         [] { return Status{}; }});
    status = parts.Feed(body);
    if (status) status = parts.Finish();
    if (!status) return Error(400, "invalid_request", status.message());
    next.explicit_session = session;
    if (session && !io::ParseJson(configuration, &next.session, true,
                                  {64, 65536, body.size(), true}))
      return Error(400, "invalid_request", "Invalid WebRTC session JSON");
  } else {
    return Error(415, "unsupported_media_type",
                 "Expected SDP or multipart form");
  }
  if (next.sdp.empty() || next.sdp.find('\0') != next.sdp.npos)
    return Error(400, "live_invalid_sdp", "Missing or invalid SDP offer");
  ChatPlanError error;
  const auto valid = ValidateRealtimeWireShape(
      next.session, RealtimeShape::kSession, &error, "session");
  if (!valid) {
    auto reply = Error(error.http_status, error.code, valid.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  *offer = std::move(next);
  return {};
}
}  // namespace q4t::server
