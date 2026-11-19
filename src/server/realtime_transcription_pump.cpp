#include "q4t/server/realtime_transcription_pump.h"

#include <algorithm>

namespace q4t::server {
RealtimeTranscriptionPump::RealtimeTranscriptionPump(
    std::string id, WebSocketOutput& output,
    RealtimeTranscriptionCallbacks callbacks, uint32_t max_audio_seconds,
    size_t message_bytes)
    : message_bytes_(message_bytes) {
  const auto original = std::move(callbacks.interrupted);
  callbacks.interrupted = [this, original] {
    return stopped_.load() || (original && original());
  };
  session_ = std::make_unique<RealtimeTranscriptionSession>(
      std::move(id), output, std::move(callbacks), max_audio_seconds,
      message_bytes);
}
RealtimeTranscriptionPump::~RealtimeTranscriptionPump() {
  Stop();
  Join();
}
bool RealtimeTranscriptionPump::Start() {
  if (started_ || stopped_.load() || !session_->Start()) return false;
  try {
    worker_ = std::thread([this] { Run(); });
    started_ = true;
    return true;
  } catch (...) {
    Stop();
    return false;
  }
}
bool RealtimeTranscriptionPump::Message(WebSocketOpcode opcode,
                                        std::string_view message) {
  if (!started_ || message.size() > message_bytes_) return false;
  std::lock_guard lock(mutex_);
  if (stopped_.load() || count_ == kRealtimeInboxMessages ||
      message.size() > kRealtimeInboxBytes - bytes_)
    return false;
  try {
    std::string text(message);
    if (text.capacity() > kRealtimeInboxBytes - bytes_) return false;
    const auto charge = text.capacity();
    messages_.push_back({opcode, std::move(text), charge});
    bytes_ += charge;
    ++count_;
  } catch (...) {
    return false;
  }
  changed_.notify_one();
  return true;
}
bool RealtimeTranscriptionPump::CanAccept(size_t message_bytes) const {
  // Invalid/oversized messages go through Message and close explicitly, rather
  // than waiting forever for a capacity that this connection cannot provide.
  if (message_bytes > message_bytes_) return true;
  std::lock_guard lock(mutex_);
  return stopped_.load() ||
         (count_ < kRealtimeInboxMessages &&
          std::max(message_bytes, std::string{}.capacity()) <=
              kRealtimeInboxBytes - bytes_);
}
void RealtimeTranscriptionPump::Stop() noexcept {
  stopped_.store(true);
  changed_.notify_one();
}
void RealtimeTranscriptionPump::Join() noexcept {
  if (worker_.joinable()) worker_.join();
}
size_t RealtimeTranscriptionPump::InputBytes() const {
  std::lock_guard lock(mutex_);
  return bytes_;
}
size_t RealtimeTranscriptionPump::InputMessages() const {
  std::lock_guard lock(mutex_);
  return count_;
}
void RealtimeTranscriptionPump::Run() noexcept {
  try {
    while (!stopped_.load()) {
      MessageItem item{};
      bool received = false;
      {
        std::unique_lock lock(mutex_);
        if (messages_.empty())
          changed_.wait_for(lock, std::chrono::milliseconds(5), [&] {
            return stopped_.load() || !messages_.empty();
          });
        if (stopped_.load()) break;
        if (!messages_.empty()) {
          item = std::move(messages_.front());
          messages_.pop_front();
          received = true;
        }
      }
      if (received) {
        const bool ok = session_->Message(item.opcode, item.text);
        std::string{}.swap(item.text);
        {
          // Keep the active string charged until parsing/processing completes.
          std::lock_guard lock(mutex_);
          bytes_ -= item.charge;
          --count_;
        }
        if (!ok || stopped_.load()) break;
      }
      if (!session_->Tick()) break;
    }
  } catch (...) {
    // The socket owner observes Alive=false and closes with application error;
    // never publish completion from an abandoned partial command.
  }
  Stop();
  session_->Cancel();
  session_->Join();
  std::lock_guard lock(mutex_);
  messages_.clear();
  bytes_ = count_ = 0;
}
}  // namespace q4t::server
