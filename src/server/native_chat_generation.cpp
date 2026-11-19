#include "q4t/server/native_chat_generation.h"

#include "object_protocol.h"

namespace q4t::server {
Status NativeChatGeneration::Emit(const ResponseDelta& delta) {
  if (sealed_) return Status::Fail("Native output owner is closed");
  if (!error_.ok()) return Status::Fail(error_.ClientMessage());
  if (!Valid()) {
    error_ = object_detail::Error(500, "native_output_owner_invalid",
                                  "Native output owner is invalid");
    return Status::Fail(error_.ClientMessage());
  }
  auto remaining = maximum_bytes_ - accepted_bytes_;
  for (const auto* text :
       {&delta.text, &delta.call.id, &delta.call.name, &delta.call.arguments}) {
    if (text->size() > remaining) {
      error_ = object_detail::Error(503, "native_output_capacity_exhausted",
                                    "Native output capacity exhausted");
      return Status::Fail(error_.ClientMessage());
    }
    remaining -= text->size();
  }
  auto reply = sink_(delta);
  if (!reply.ok() || reply.http_status < 200 || reply.http_status >= 300 ||
      reply.commit_unknown) {
    if (reply.code.empty()) {
      reply.code = "native_sink_result_invalid";
      reply.message = "Native output sink did not confirm acceptance";
      reply.http_status = 503;
    }
    error_ = std::move(reply);
    return Status::Fail(error_.ClientMessage());
  }
  accepted_bytes_ = maximum_bytes_ - remaining;
  return {};
}
}  // namespace q4t::server
