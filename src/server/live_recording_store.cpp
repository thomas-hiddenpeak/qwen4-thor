#include "q4t/server/live_recording_store.h"

#include <utility>

#include "object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
constexpr size_t kQueueBytes = 16U << 20, kQueueBlocks = 256;
ObjectReply Capacity() {
  return Error(503, "live_recording_capacity",
               "Live recording buffer exhausted");
}
}  // namespace

LiveRecordingCapture::LiveRecordingCapture(
    ObjectStore& objects, std::string scope, std::string id,
    RealtimeAudioFormat format, std::function<bool()> revoked,
    std::unique_ptr<HttpBodyLease> memory)
    : memory_(std::move(memory)),
      objects_(objects),
      binary_(objects),
      scope_(std::move(scope)),
      id_(std::move(id)),
      format_(format),
      revoked_(std::move(revoked)) {
  try {
    worker_ = std::thread(&LiveRecordingCapture::Run, this);
  } catch (...) {
    done_ = true;
    error_ = Capacity();
  }
}
LiveRecordingCapture::~LiveRecordingCapture() {
  Cancel();
  Join();
}
void LiveRecordingCapture::Audio(LiveAudioChannel channel, uint64_t ticks,
                                 std::string_view bytes) {
  std::lock_guard lock(mutex_);
  if (done_ || stopped_ || finishing_ || bytes.empty()) return;
  try {
    if (queue_.size() < kQueueBlocks && bytes.size() <= kQueueBytes - bytes_) {
      queue_.push_back({channel, ticks, std::string(bytes)});
      bytes_ += bytes.size();
      ready_.notify_one();
      return;
    }
  } catch (const std::bad_alloc&) {
  }
  error_ = Capacity();
  stopped_ = true;
  ready_.notify_one();
}
void LiveRecordingCapture::Finish(uint64_t ticks) {
  std::lock_guard lock(mutex_);
  if (finishing_ || done_ || stopped_) return;
  finishing_ = true;
  end_ticks_ = ticks;
  ready_.notify_one();
}
void LiveRecordingCapture::Cancel() {
  std::lock_guard lock(mutex_);
  stopped_ = true;
  ready_.notify_one();
}
void LiveRecordingCapture::Join() {
  if (worker_.joinable()) worker_.join();
}
bool LiveRecordingCapture::Drained() const {
  std::lock_guard lock(mutex_);
  return done_ && error_.ok();
}
ObjectReply LiveRecordingCapture::TakeError() {
  std::lock_guard lock(mutex_);
  if (!done_) return {};
  return std::exchange(error_, {});
}
BinaryAccess LiveRecordingCapture::Access() {
  return {scope_, Clock::now() + 3s,
          [this] { return stopped_.load() || (revoked_ && revoked_()); }};
}
ObjectReply LiveRecordingCapture::Store(BinaryStage* stage) {
  auto result =
      binary_.Begin(Access(), uint64_t(format_.rate) * 4 * 3600, 3600, stage);
  if (!result.ok()) return result;
  LiveRecordingWriter writer(format_, [&](std::string_view bytes) {
    return binary_.Append(Access(), stage, bytes);
  });
  uint64_t end = 0;
  for (;;) {
    Block block;
    {
      std::unique_lock lock(mutex_);
      ready_.wait(lock,
                  [&] { return stopped_ || !queue_.empty() || finishing_; });
      if (stopped_) return {};
      if (queue_.empty()) {
        end = end_ticks_;
        break;
      }
      block = std::move(queue_.front());
      queue_.pop_front();
      bytes_ -= block.bytes.size();
    }
    result = writer.Audio(block.channel, block.ticks, block.bytes);
    if (!result.ok()) return result;
  }
  LiveRecordingLayout layout;
  result = writer.Finish(end, &layout);
  if (!result.ok()) return result;
  ObjectRequest root;
  root.operation = ObjectOperation::kPut;
  root.kind = ObjectKind::kLiveRecording;
  root.scope = scope_;
  root.id = id_;
  root.data = LiveRecordingMetadata(layout);
  return binary_.Publish(Access(), stage, std::move(root));
}
void LiveRecordingCapture::Run() noexcept {
  BinaryStage stage;
  ObjectReply result;
  try {
    result = Store(&stage);
  } catch (...) {
    result = Error(503, "live_recording_failed", "Live recording failed");
  }
  if (!stage.closed && !stage.commit_unknown && !stage.id.empty()) {
    try {
      if (!binary_.Abort({scope_, Clock::now() + 500ms, {}}, &stage).ok())
        objects_.RecordAuxiliaryCleanupFailure();
    } catch (...) {
      objects_.RecordAuxiliaryCleanupFailure();
    }
  }
  std::lock_guard lock(mutex_);
  queue_.clear();
  bytes_ = 0;
  if (!stopped_ && error_.ok()) error_ = std::move(result);
  done_ = true;
}

void SendLiveRecording(int fd, ObjectStore& objects, const BinaryAccess& access,
                       const std::string& id) {
  BinaryStore binary(objects);
  BinaryRead pin;
  Json data;
  auto result =
      binary.Open(access, ObjectKind::kLiveRecording, id, 3600, &pin, &data);
  ScopeExit close([&] {
    if (pin.id.empty() || pin.commit_unknown) return;
    try {
      if (!binary.Close({access.scope, Clock::now() + 500ms, {}}, &pin).ok())
        objects.RecordAuxiliaryCleanupFailure();
    } catch (...) {
      objects.RecordAuxiliaryCleanupFailure();
    }
  });
  LiveRecordingLayout layout;
  if (result.ok() && !ReadLiveRecordingLayout(data, pin.manifest, &layout))
    result = Error(503, "invalid_recording", "Invalid stored recording layout");
  if (!result.ok()) {
    detail::SendError(fd, result.http_status, result.ClientMessage(),
                      {access.deadline}, result.ClientCode());
    return;
  }
  LiveRecordingWaveReader reader(std::move(layout),
                                 [&](size_t i, std::string* bytes) {
                                   return binary.Read(access, pin, i, bytes);
                                 });
  std::string first, samples;
  result = reader.Next(&first);
  if (result.ok() && reader.Bytes() > first.size())
    result = reader.Next(&samples);
  if (!result.ok()) {
    detail::SendError(fd, result.http_status, result.ClientMessage(),
                      {access.deadline}, result.ClientCode());
    return;
  }
  first += samples;
  detail::SendBinaryResponse(
      fd, reader.Bytes(),
      [&](std::string* bytes) {
        if (access.cancelled && access.cancelled()) return false;
        if (!first.empty()) {
          *bytes = std::move(first);
          first.clear();
          return true;
        }
        const auto reply = reader.Next(bytes);
        if (!reply.ok())
          if (auto* context = detail::HttpResponseContext::Current(fd))
            context->ObserveError(reply.http_status, reply.ClientMessage(),
                                  reply.ClientCode());
        return reply.ok();
      },
      {access.deadline}, "audio/wav");
}
}  // namespace q4t::server
