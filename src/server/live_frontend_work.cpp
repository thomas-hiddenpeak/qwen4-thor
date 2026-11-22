#include "q4t/server/live_frontend_work.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/realtime_chat_plan.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Failure(std::string_view code, std::string_view message) {
  return Error(503, std::string(code), std::string(message));
}
RealtimeResponseOutputOptions Options(const RealtimeConversationJob& job,
                                      std::string conversation_id) {
  RealtimeResponseOutputOptions options;
  options.response_id = job.response_id;
  options.conversation_id = std::move(conversation_id);
  options.in_conversation = job.in_conversation;
  // The original native speech receipt remains 24k PCM, including the full
  // finite Chat message check. Live delivery converts only after that binding.
  options.audio_format = RealtimeAudioFormat{};
  return options;
}
}  // namespace

LiveFrontendWork::LiveFrontendWork(RealtimeConversationJob job,
                                   std::string conversation_id,
                                   RealtimeAudioFormat format,
                                   LiveFrontendCallbacks callbacks,
                                   std::string client_event)
    : job_(std::move(job)),
      format_(format),
      callbacks_(std::move(callbacks)),
      client_event_(std::move(client_event)),
      output_(
          Options(job_, std::move(conversation_id)),
          [](const Json&) { return ObjectReply{}; }, callbacks_.item,
          [](std::string_view prefix) {
            return detail::NewRequestId(std::string(prefix));
          },
          callbacks_.audio) {}
LiveFrontendWork::~LiveFrontendWork() {
  Cancel();
  Join();
}
ObjectReply LiveFrontendWork::Start() {
  if (attempted_ || cancelled_ || finished_ || !callbacks_.prepare ||
      !callbacks_.acquire || !callbacks_.release || !callbacks_.execute ||
      !callbacks_.emit || !callbacks_.timeline || !callbacks_.item ||
      !callbacks_.audio || !ValidRequestId(job_.response_id))
    return Failure("live_frontend_unavailable", "Native frontend is not bound");
  attempted_ = true;
  if (job_.prompt || job_.mcp || job_.mcp_definitions_only ||
      !job_.snapshot.pending_inputs.empty() ||
      !job_.snapshot.pending_outputs.empty() ||
      job_.snapshot.tickets.capacity() > RealtimeConversation::kMaximumItems ||
      job_.snapshot.owned_mcp.capacity() >
          RealtimeConversation::kMaximumItems ||
      job_.snapshot.pending_inputs.capacity() >
          RealtimeConversation::kMaximumItems ||
      job_.snapshot.pending_outputs.capacity() >
          RealtimeConversation::kMaximumItems ||
      job_.snapshot.owner.capacity() > 256 ||
      job_.response_id.capacity() > 256 ||
      !LiveAudioOutput(format_, 0, callbacks_.emit, {}, client_event_).Valid())
    return Failure("live_frontend_snapshot_invalid",
                   "Frontend requires resolved native conversation input");
  for (const auto& ticket : job_.snapshot.tickets)
    if (ticket.owner.capacity() > 256 || ticket.id.capacity() > 256)
      return Failure("live_frontend_snapshot_invalid",
                     "Frontend ownership metadata exceeds its limit");
  size_t total = 0;
  for (const auto* value :
       {&job_.settings, &job_.response, &job_.snapshot.items}) {
    size_t charge = 0;
    if (!MeasureRealtimeStorage(*value, (96ULL << 20) - total, &charge))
      return Failure("live_frontend_capacity",
                     "Frontend snapshot is too large");
    total += charge;
  }
  RealtimeSpeechOptions speech;
  ChatPlanError error;
  if (!PlanRealtimeSpeech(job_.settings, job_.response, &speech, &error) ||
      !speech.enabled || speech.format != RealtimeAudioFormat{})
    return Failure("live_frontend_audio_invalid",
                   "Native frontend must supply verified 24k PCM");
  try {
    auto control = callbacks_.acquire(job_.response_id);
    if (!control || control->id() != job_.response_id)
      return Failure("live_frontend_owner_invalid", "Request ownership failed");
    control_ = std::move(control);
    auto reply = output_.Begin();
    if (!Accepted(reply)) {
      Release();
      return reply;
    }
    result_.reply = Failure("live_frontend_execution_failed",
                            "Native frontend execution failed");
    worker_ = std::thread([this] { Run(); });
    started_ = true;
    return {};
  } catch (...) {
    Release();
    return Failure("live_frontend_start_failed", "Could not start frontend");
  }
}
ObjectReply LiveFrontendWork::Push(const ResponseDelta& delta) {
  for (const auto* text :
       {&delta.text, &delta.call.id, &delta.call.name, &delta.call.arguments})
    if (text->size() > kMailboxBytes)
      return Failure("live_frontend_delta_limit", "Model delta is too large");
  ResponseDelta copy = delta;
  size_t charge = sizeof(Queued);
  for (const auto* text :
       {&copy.text, &copy.call.id, &copy.call.name, &copy.call.arguments}) {
    if (text->capacity() > kMailboxBytes - charge)
      return Failure("live_frontend_delta_limit", "Model delta is too large");
    charge += text->capacity();
  }
  return Enqueue({std::move(copy), charge});
}
ObjectReply LiveFrontendWork::Push(const RealtimeSpeechEvent& event) {
  if (event.bytes.size() > RealtimeSpeechEncoder::kMaximumChunkBytes ||
      event.transcript.size() > 1ULL << 20)
    return Failure("live_frontend_delta_limit", "Speech delta is too large");
  auto copy = event;
  const auto charge =
      sizeof(Queued) + copy.bytes.capacity() + copy.transcript.capacity();
  if (charge > kMailboxBytes)
    return Failure("live_frontend_delta_limit", "Speech delta is too large");
  return Enqueue({std::move(copy), charge});
}
ObjectReply LiveFrontendWork::Enqueue(Queued queued) {
  const auto deadline =
      std::min(control_->deadline(), Clock::now() + std::chrono::seconds(15));
  std::unique_lock lock(mutex_);
  while (count_ >= kMailboxItems || queued.charge > kMailboxBytes - bytes_) {
    if (control_->Cancelled() || Clock::now() >= deadline)
      return Failure("live_frontend_backpressure", "Frontend output stalled");
    changed_.wait_for(lock, std::chrono::milliseconds(5));
  }
  if (control_->Cancelled())
    return Error(409, control_->Reason(), "Frontend cancelled");
  const auto charge = queued.charge;
  queue_.push_back(std::move(queued));
  bytes_ += charge;
  ++count_;
  return {};
}
void LiveFrontendWork::Run() noexcept {
  try {
    auto reply = callbacks_.prepare(job_);
    if (Accepted(reply) && !control_->Cancelled())
      result_ = callbacks_.execute(
          job_, control_, [&](const auto& delta) { return Push(delta); },
          [&](const auto& speech) { return Push(speech); });
    else
      result_.reply = Accepted(reply)
                          ? Error(409, control_->Reason(), "Frontend cancelled")
                          : std::move(reply);
  } catch (...) {
    // Keep the original failure and the actual borrowed owner until join.
  }
  done_.store(true, std::memory_order_release);
}
ObjectReply LiveFrontendWork::Deliver(Queued& queued) {
  if (auto* delta = std::get_if<ResponseDelta>(&queued.value))
    return output_.Append(*delta);
  const auto& speech = std::get<RealtimeSpeechEvent>(queued.value);
  if (!audio_) {
    audio_ = std::make_unique<LiveAudioOutput>(
        format_, callbacks_.timeline(), callbacks_.emit,
        [this] { return control_->Cancelled(); }, client_event_,
        callbacks_.record);
    if (!audio_->Valid())
      return Failure("live_frontend_audio_invalid", "Invalid output timeline");
  }
  auto reply = speech.kind == RealtimeSpeechEvent::Kind::kChunk
                   ? output_.AppendAudio(speech.bytes)
                   : output_.CompleteAudio(speech.receipt, speech.transcript);
  return Accepted(reply) ? audio_->Append(speech) : reply;
}
ObjectReply LiveFrontendWork::Tick() {
  if (!started_)
    return Failure("live_frontend_not_started", "Frontend has not started");
  if (finished_) return {};
  try {
    for (size_t count = 0; count < 64; ++count) {
      Queued next;
      {
        std::lock_guard lock(mutex_);
        if (cancelled_) {
          queue_.clear();
          bytes_ = count_ = 0;
          changed_.notify_all();
          break;
        }
        if (queue_.empty()) break;
        next = std::move(queue_.front());
        queue_.pop_front();
      }
      ObjectReply reply;
      try {
        reply = Deliver(next);
      } catch (...) {
        reply = Failure("live_frontend_delivery_failed",
                        "Frontend callback failed");
      }
      {
        // Keep the active variant charged while callbacks borrow it.
        std::lock_guard lock(mutex_);
        bytes_ -= next.charge;
        --count_;
      }
      changed_.notify_all();
      if (!Accepted(reply)) {
        delivery_error_ = std::move(reply);
        Cancel();
        break;
      }
    }
    if (!done_.load(std::memory_order_acquire)) return {};
    {
      std::lock_guard lock(mutex_);
      if (!queue_.empty()) return {};
    }
    if (worker_.joinable()) worker_.join();
    const bool cancelled = cancelled_ || control_->Cancelled();
    if (!Accepted(delivery_error_)) result_.reply = delivery_error_;
    if (cancelled && Accepted(result_.reply))
      result_.reply = Error(
          409, control_->Cancelled() ? control_->Reason() : "explicit_cancel",
          "Frontend cancelled");
    if (!Accepted(result_.reply))
      result_.state = cancelled && Accepted(delivery_error_)
                          ? RealtimeResponseEnd::State::kCancelled
                          : RealtimeResponseEnd::State::kFailed;
    RealtimeResponseEnd end;
    end.state = result_.state;
    end.reason = result_.reason;
    end.usage = result_.usage;
    end.error_code = std::string(result_.reply.ClientCode());
    end.error_message = result_.reply.ClientMessage();
    if (Accepted(result_.reply)) end.message = &result_.message;
    auto reply = output_.Finish(end);
    if (!Accepted(reply)) {
      result_.reply = std::move(reply);
      result_.state = RealtimeResponseEnd::State::kFailed;
      // A verifier failure may precede any terminal history mutation. Seal
      // those actual items as failed; the formatter refuses to replay a sink
      // whose commit was unsuccessful or uncertain.
      end.state = result_.state;
      end.message = nullptr;
      end.error_code = std::string(result_.reply.ClientCode());
      end.error_message = result_.reply.ClientMessage();
      if (!output_.Terminal()) (void)output_.Finish(end);
    }
    Release();
    finished_ = true;
    return {};
  } catch (...) {
    Cancel();
    return Failure("live_frontend_delivery_failed", "Frontend delivery failed");
  }
}
void LiveFrontendWork::Cancel() noexcept {
  cancelled_ = true;
  if (control_) control_->Cancel();
  if (audio_) audio_->Cancel();
  changed_.notify_all();
}
void LiveFrontendWork::Release() noexcept {
  if (!control_) return;
  try {
    callbacks_.release(control_);
  } catch (...) {
  }
  control_.reset();
}
void LiveFrontendWork::Join() noexcept {
  if (worker_.joinable()) worker_.join();
  Release();
  std::lock_guard lock(mutex_);
  queue_.clear();
  bytes_ = count_ = 0;
}
size_t LiveFrontendWork::QueuedBytes() const {
  std::lock_guard lock(mutex_);
  return bytes_;
}
}  // namespace q4t::server
