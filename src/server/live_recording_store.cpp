#include "q4t/server/live_recording_store.h"

#include <utility>

#include "object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/realtime_conversation.h"
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
void LiveRecordingCapture::Finish(uint64_t ticks, const CaptureState& capture) {
  std::lock_guard lock(mutex_);
  if (finishing_ || done_ || stopped_) return;
  finishing_ = true;
  end_ticks_ = ticks;
  if (capture) {
    try {
      // The worker serializes and transacts the capture while the immutable
      // copy is resident. The dedicated lease follows it through publication.
      size_t bytes = 0;
      if (!memory_ ||
          memory_->GrowTo(kLiveRecordingHostBytes + 3 * kLiveForkStateBytes)) {
        fork_error_ = capture(&fork_state_);
        if (fork_error_.ok() &&
            MeasureRealtimeStorage(fork_state_, kLiveForkStateBytes, &bytes)) {
          ready_.notify_one();
          return;
        }
      }
      if (fork_error_.ok())
        fork_error_ = Error(503, "live_fork_storage_capacity",
                            "Insufficient space for conversation state");
    } catch (...) {
      fork_error_ = Error(503, "live_fork_history_unavailable",
                          "Could not capture conversation state");
    }
    fork_state_ = {};
  }
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
  if (!fork_state_.IsNull() && fork_error_.ok()) {
    // Publish audio and private continuation state in one scoped transaction.
    // WAV reads never have to hydrate model context or its string payloads.
    ObjectRequest checkpoint;
    checkpoint.operation = ObjectOperation::kPut;
    checkpoint.kind = ObjectKind::kLiveForkState;
    checkpoint.scope = scope_;
    checkpoint.id = "fork_" + Digest(id_);
    checkpoint.data = std::move(fork_state_);
    Set(&checkpoint.data, "source_id", String(id_));
    auto with_state = root;
    Set(&with_state.data, "fork_state_id", String(checkpoint.id));
    auto publication = binary_.Publish(Access(), stage, std::move(with_state),
                                       {&checkpoint, 1});
    if (publication.ok() || publication.commit_unknown ||
        (publication.http_status != 413 && publication.http_status != 429))
      return publication;
    fork_error_ =
        Error(503, "live_fork_storage_capacity",
              "Recording retained without fork history: capacity exhausted");
  }
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
  if (!stopped_ && error_.ok())
    error_ = result.ok() ? std::move(fork_error_) : std::move(result);
  done_ = true;
}

ObjectReply ReadLiveForkState(ObjectStore& objects, const BinaryAccess& access,
                              const std::string& source) {
  ObjectRequest request;
  request.kind = ObjectKind::kLiveRecording;
  request.id = source;
  request.scope = access.scope;
  auto recording = objects.Execute(request, access.deadline, access.cancelled);
  if (!recording.ok()) return recording;
  const auto* data = recording.value.Find("data");
  if (!data || data->GetString("format") != "q4t.live_recording.v1")
    return Error(503, "invalid_recording", "Invalid recording metadata");
  const auto id = data->GetString("fork_state_id");
  if (id.empty())
    return Error(409, "live_fork_history_unavailable",
                 "Recording has no saved conversation state");
  if (id != "fork_" + Digest(source))
    return Error(503, "invalid_recording", "Invalid recording history link");
  request.kind = ObjectKind::kLiveForkState;
  request.id = id;
  auto stored = objects.Execute(request, access.deadline, access.cancelled);
  if (stored.http_status == 404)
    return Error(503, "invalid_recording",
                 "Saved conversation state is missing");
  if (!stored.ok()) return stored;
  auto* state =
      stored.value.Has("data") ? &Field(stored.value, "data") : nullptr;
  if (!state || state->GetString("format") != "q4t.live_fork.v1" ||
      state->GetString("source_id") != source ||
      !state->GetArray("frontend_history") ||
      !state->GetArray("decision_items") || !state->Find("configuration") ||
      !state->Find("configuration")->IsObject() ||
      !state->Find("configuration")->GetBool("store"))
    return Error(503, "invalid_recording", "Invalid stored conversation state");
  ObjectReply result;
  result.value = std::move(*state);
  return result;
}

void SendLiveRecording(int fd, ObjectStore& objects, const BinaryAccess& access,
                       const std::string& id, HttpBodyBudget& budget) {
  HttpBodyLease working(budget);
  if (!working.Acquire(kLiveRecordingHostBytes)) {
    detail::SendError(fd, 503, "Recording download capacity exhausted",
                      {.retry_after = 1s}, "capacity_exhausted");
    return;
  }
  const auto send_error = [&](const ObjectReply& reply) {
    detail::WriteOptions write{access.deadline};
    if (!reply.commit_unknown && (reply.code == "object_recovery_backoff" ||
                                  reply.code == "object_controller_backoff"))
      write.retry_after = 1s;
    detail::SendError(fd, reply.http_status, reply.ClientMessage(), write,
                      reply.ClientCode());
  };
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
    send_error(result);
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
    send_error(result);
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
