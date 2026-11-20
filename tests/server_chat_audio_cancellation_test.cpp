#include "q4t/server/chat_audio_output.h"

#include <chrono>

#include "q4t/server/object_store.h"
#include "q4t/server/request_control.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
ObjectReply Interrupted(const RequestControl& control) {
  ObjectReply reply;
  reply.http_status = 409;
  reply.code = control.Reason();
  return reply;
}
}  // namespace

Q4T_TEST(
    chat_audio_cancellation_keeps_disconnect_explicit_shutdown_and_deadline) {
  for (auto reason :
       {RequestControl::State::kDisconnect, RequestControl::State::kExplicit,
        RequestControl::State::kShutdown, RequestControl::State::kDeadline}) {
    RequestControl control("audio", "secret", Clock::now() + 1min);
    Q4T_CHECK(control.Cancel(reason));
    Q4T_CHECK(ChatAudioWasCancelled(Interrupted(control), &control, true));
    auto tokenizer = Interrupted(control);
    tokenizer.code = "tts_cancelled";
    Q4T_CHECK(ChatAudioWasCancelled(tokenizer, &control, true));
    Q4T_CHECK(control.Cancel(reason));
    Q4T_CHECK(ChatAudioWasCancelled(Interrupted(control), &control, true));
  }
  return true;
}

Q4T_TEST(chat_audio_cancellation_requires_actual_owner_and_cancelled_reply) {
  RequestControl control("audio", "secret", Clock::now() + 1min);
  ObjectReply reply;
  reply.http_status = 409;
  reply.code = "explicit_cancel";
  Q4T_CHECK(!ChatAudioWasCancelled(reply, &control, true));
  Q4T_CHECK(!ChatAudioWasCancelled(reply, nullptr, true));
  Q4T_CHECK(control.Cancel());
  Q4T_CHECK(ChatAudioWasCancelled(reply, &control, true));
  for (const auto* code : {"", "tts_compute_failed", "voice_consent_revoked",
                           "object_revision_conflict"}) {
    reply.code = code;
    Q4T_CHECK(!ChatAudioWasCancelled(reply, &control, true));
  }
  return true;
}

Q4T_TEST(chat_audio_cancellation_does_not_hide_device_or_backend_failure) {
  RequestControl control("audio", "secret", Clock::now() + 1min);
  Q4T_CHECK(control.Cancel(RequestControl::State::kDisconnect));
  auto reply = Interrupted(control);
  Q4T_CHECK(!ChatAudioWasCancelled(reply, &control, false));
  for (int status : {200, 400, 429, 500, 502, 503}) {
    reply.http_status = status;
    Q4T_CHECK(!ChatAudioWasCancelled(reply, &control, true));
  }
  RequestControl failed("failed", "secret", Clock::now() + 1min);
  Q4T_CHECK(failed.Cancel(RequestControl::State::kFailure));
  Q4T_CHECK(!ChatAudioWasCancelled(Interrupted(failed), &failed, true));
  return true;
}

Q4T_TEST(chat_audio_cancellation_keeps_unknown_commit_and_finished_owner) {
  RequestControl control("audio", "secret", Clock::now() + 1min);
  Q4T_CHECK(control.Cancel());
  auto reply = Interrupted(control);
  reply.commit_unknown = true;
  Q4T_CHECK(!ChatAudioWasCancelled(reply, &control, true));
  RequestControl finished("finished", "secret", Clock::now() + 1min);
  Q4T_CHECK(finished.Finish());
  reply.commit_unknown = false;
  Q4T_CHECK(!ChatAudioWasCancelled(reply, &finished, true));
  return true;
}
