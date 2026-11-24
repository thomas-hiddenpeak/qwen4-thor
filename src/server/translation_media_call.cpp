#include "translation_media_call.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/http_response.h"
#include "q4t/server/realtime_transcription_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
constexpr size_t kInputBytes = 2ULL << 20, kInputItems = 512;
constexpr std::string_view kAppend =
    R"({"type":"session.input_audio_buffer.append","audio":")";
}  // namespace

TranslationMediaCall::TranslationMediaCall(RealtimeTranslationNative native,
                                           uint32_t max_seconds,
                                           size_t message_bytes, Send send,
                                           bool expect_channel)
    : send_(std::move(send)),
      message_bytes_(message_bytes),
      batch_bytes_(
          message_bytes > kAppend.size() + 2
              ? std::min<size_t>(9600,
                                 (message_bytes - kAppend.size() - 2) / 4 * 3) &
                    ~size_t{1}
              : 0),
      channel_(expect_channel),
      session_(std::make_shared<RealtimeTranslationSession>(
          detail::NewRequestId("sess_"),
          [this](auto event) { return Publish(std::move(event)); },
          std::move(native), message_bytes, max_seconds)),
      pump_(session_, message_bytes) {}

TranslationMediaCall::~TranslationMediaCall() {
  EndTransport();
  pump_.Join();
}

ObjectReply TranslationMediaCall::Start(const Json& configuration) {
  if (!send_ || !batch_bytes_)
    return Error(400, "invalid_request", "Media message budget is too small");
  expires_ = Clock::now() + 59min;
  return pump_.Start(&configuration);
}

bool TranslationMediaCall::Publish(Json event) {
  const auto type = event.GetString("type");
  if (type == "session.output_audio.delta") {
    std::string pcm;
    const auto* delta = event.Find("delta");
    if (!delta || !delta->IsString() ||
        !DecodeRealtimeAudioBase64(delta->str, &pcm).ok() || pcm.empty() ||
        pcm.size() % 2 || pcm.size() > MediaPcmPlayout::kMaxBytes)
      return false;
    std::unique_lock lock(output_mutex_);
    return writable_.wait_for(lock, 15s, [&] {
      return ended_ || playout_.Enqueue(std::move(pcm));
    }) && !ended_;
  }
  auto wire = OutputJson(event);
  std::lock_guard lock(output_mutex_);
  if (ended_) return false;
  // A receiver may close the peer connection as soon as it sees this event.
  if (type == "session.closed")
    closed_event_ = std::move(wire);
  else
    channel_.Queue(wire);
  return true;
}

bool TranslationMediaCall::Buffer(bool audio, std::string_view bytes) {
  if (bytes.size() > kInputBytes - input_bytes_) return false;
  if (!audio) {
    if (input_.size() >= kInputItems || bytes.size() > message_bytes_)
      return false;
    input_.push_back({false, std::string(bytes)});
    input_bytes_ += bytes.size();
    return true;
  }
  if (bytes.empty() || bytes.size() % 2) return false;
  while (!bytes.empty()) {
    if (input_.empty() || !input_.back().audio ||
        input_.back().bytes.size() == batch_bytes_) {
      if (input_.size() >= kInputItems) return false;
      input_.push_back({true, {}});
      batch_started_ = Clock::now();
    }
    auto& batch = input_.back().bytes;
    const auto count = std::min(bytes.size(), batch_bytes_ - batch.size());
    batch.append(bytes.substr(0, count));
    bytes.remove_prefix(count);
    input_bytes_ += count;
  }
  return true;
}

bool TranslationMediaCall::Receive(char type, std::string_view payload) {
  if (type == 'V' && payload.empty() && !verified_) return verified_ = true;
  if (!verified_ || payload.size() > 65535) return false;
  if (type == 'P') {
    if (session_->Failed() || session_->Completed()) return true;
    return Buffer(true, payload);
  }
  std::lock_guard lock(output_mutex_);
  return channel_.Receive(type, payload,
                          [this](auto wire) { return Buffer(false, wire); });
}

void TranslationMediaCall::CloseDataChannel() {
  std::lock_guard lock(output_mutex_);
  channel_.Disconnect();
}

bool TranslationMediaCall::Submit() {
  while (!input_.empty()) {
    const auto& [audio, data] = input_.front();
    if (audio && input_.size() == 1 && data.size() < batch_bytes_ &&
        Clock::now() - batch_started_ < 200ms)
      break;
    const size_t size =
        audio ? kAppend.size() + 4 * ((data.size() + 2) / 3) + 2 : data.size();
    if (!pump_.CanAccept(size)) break;
    std::string wire;
    if (audio) {
      std::string encoded;
      if (!io::EncodeBase64(data, message_bytes_, &encoded)) return false;
      wire.reserve(size);
      wire.append(kAppend).append(encoded).append("\"}");
    }
    if (!pump_.Message(WebSocketOpcode::kText, audio ? wire : data))
      return false;
    input_bytes_ -= data.size();
    input_.pop_front();
  }
  return true;
}

bool TranslationMediaCall::Tick(Clock::time_point now) {
  if (ended_) return true;
  if (now >= expires_ || !pump_.Alive()) return false;
  if (!session_->Completed() && !session_->Failed() && !Submit()) return false;
  if (!verified_) return true;
  std::lock_guard lock(output_mutex_);
  bool writable = playout_.Flush(now, send_);
  if (writable) writable_.notify_one();
  if (playout_.Empty() && !closed_event_.empty()) {
    channel_.Queue(closed_event_);
    closed_event_.clear();
  }
  if (!channel_.Flush(now, send_)) writable = false;
  if (writable)
    blocked_since_ = {};
  else if (blocked_since_ == Clock::time_point{})
    blocked_since_ = now;
  return writable || now - blocked_since_ < 15s;
}

bool TranslationMediaCall::Finished() const {
  std::lock_guard lock(output_mutex_);
  return ended_ ||
         ((session_->Completed() || session_->Failed()) &&
          closed_event_.empty() && playout_.Empty() && channel_.Empty());
}

bool TranslationMediaCall::EndTransport() {
  {
    std::lock_guard lock(output_mutex_);
    ended_ = true;
    playout_.Clear();
  }
  writable_.notify_all();
  session_->Interrupt();
  pump_.Stop();
  return true;
}
}  // namespace q4t::server
