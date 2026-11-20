#include "q4t/server/live_frontend.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Unavailable() {
  return Error(503, "live_frontend_unavailable", "Frontend is not active");
}
ObjectReply Invalid(std::string_view message) {
  return Error(400, "live_frontend_invalid", std::string(message));
}
ObjectReply Capacity() {
  return Error(429, "live_frontend_capacity", "Frontend queue is full");
}
}  // namespace

LiveFrontend::LiveFrontend(std::string id, LiveFrontendNative native,
                           uint32_t max_audio_seconds, size_t message_bytes)
    : id_(std::move(id)),
      native_(std::move(native)),
      history_(id_),
      input_(id_ + "_input", history_, native_.input, InputCallbacks(),
             max_audio_seconds, message_bytes) {}
LiveFrontend::~LiveFrontend() {
  Cancel();
  Join();
}
std::chrono::steady_clock::time_point LiveFrontend::Now() const {
  return native_.now ? native_.now() : std::chrono::steady_clock::now();
}
bool LiveFrontend::Active() const {
  return prepared_ && !closing_ && !cancelled_ && !failed_;
}
uint64_t LiveFrontend::Timeline() const {
  if (!prepared_) return 0;
  const auto elapsed =
      std::chrono::duration_cast<std::chrono::microseconds>(Now() - started_)
          .count();
  const auto ticks =
      elapsed <= 0 ? 0 : std::min<uint64_t>(elapsed, 3600000000ULL) * 48 / 1000;
  return std::min(kRealtimeSessionTicks,
                  std::max({ticks, input_.AcceptedTicks(), output_ticks_}));
}
LiveAudioInputCallbacks LiveFrontend::InputCallbacks() {
  LiveAudioInputCallbacks callbacks;
  callbacks.emit = [this](Json event) {
    return native_.emit(std::move(event));
  };
  callbacks.ready = [this] { return !work_; };
  callbacks.speech = [this](bool started, auto, auto, bool interrupt) {
    if (started && interrupt) {
      if (work_) work_->Cancel();
      DropQueued();
    }
    return !cancelled_ && !failed_;
  };
  callbacks.capture_response = [this](RealtimePromptSelection* selection) {
    if (!selection) return Unavailable();
    selection->settings = settings_;
    return ObjectReply{};
  };
  callbacks.committed = [this](RealtimeInputCommit notice) {
    return Committed(std::move(notice));
  };
  return callbacks;
}
ObjectReply LiveFrontend::Prepare(Json settings, const Json& initial,
                                  RealtimeAudioFormat format) {
  if (attempted_ || cancelled_ || !history_.Valid() || !native_.emit ||
      !native_.prepare || !native_.acquire || !native_.release ||
      !native_.execute || !native_.completed || !initial.IsArray() ||
      initial.array.size() > RealtimeConversation::kMaximumItems)
    return Unavailable();
  attempted_ = true;
  size_t charge = 0;
  if (!MeasureRealtimeStorage(initial, kSnapshotBytes, &charge))
    return Capacity();
  ChatPlanError error;
  if (!PlanRealtimeSpeech(settings, Object(), &speech_, &error) ||
      !speech_.enabled || speech_.format != RealtimeAudioFormat{} ||
      !LiveAudioOutput(format, 0, native_.emit).Valid())
    return Invalid("Native frontend requires verified 24k speech");
  for (const auto& item : initial.array) {
    RealtimeItemTicket ticket;
    auto copy = item;
    // Incoming IDs never become authority over native audio/model entries.
    Set(&copy, "id", String(detail::NewRequestId("item_")));
    const auto status =
        history_.Create(copy, copy.GetString("id"), {}, &ticket);
    if (!status) return Invalid(status.message());
  }
  Turn preview;
  auto reply = Capture(&preview, settings);
  if (!Accepted(reply)) return reply;
  reply = native_.prepare(preview.job);
  if (!Accepted(reply)) return reply;
  reply = input_.Prepare(format);
  if (!Accepted(reply)) return reply;
  settings_ = std::move(settings);
  format_ = format;
  started_ = Now();
  prepared_ = true;
  return {};
}
ObjectReply LiveFrontend::Charge(Turn* turn) {
  size_t total = sizeof(Turn) + turn->client_event.capacity();
  if (turn->client_event.size() > 256)
    return Invalid("Client event ID too long");
  for (const auto* value :
       {&turn->job.settings, &turn->job.response, &turn->job.snapshot.items}) {
    size_t bytes = 0;
    if (total > kSnapshotBytes ||
        !MeasureRealtimeStorage(*value, kSnapshotBytes - total, &bytes))
      return Capacity();
    total += bytes;
  }
  // Native ledger metadata is separately bounded by its 128 entries and IDs.
  constexpr size_t metadata = RealtimeConversation::kMaximumItems * 1024;
  if (total > kSnapshotBytes - metadata) return Capacity();
  turn->charge = total + metadata;
  return {};
}
ObjectReply LiveFrontend::Capture(Turn* turn, const Json& settings) {
  turn->job.response_id = detail::NewRequestId("resp_");
  turn->job.settings = settings;
  turn->job.response = Object();
  turn->job.native_input = true;
  const auto status = history_.Capture(nullptr, &turn->job.snapshot, true);
  return status ? Charge(turn) : Invalid(status.message());
}
ObjectReply LiveFrontend::Settings(Json settings) {
  if (!Active()) return Unavailable();
  RealtimeSpeechOptions speech;
  ChatPlanError error;
  if (!PlanRealtimeSpeech(settings, Object(), &speech, &error) ||
      !speech.enabled || !SameRealtimeVoice(speech_, speech) ||
      speech.format != speech_.format ||
      speech.chat.speech.speed != speech_.chat.speech.speed)
    return Invalid("Active frontend voice and format are immutable");
  Turn preview;
  auto reply = Capture(&preview, settings);
  if (!Accepted(reply)) return reply;
  // A settings validation cannot ask the real planner to execute unresolved
  // context. Input/settings validation is independent of pending turn work.
  preview.job.snapshot = {};
  preview.job.snapshot.items = Array();
  reply = native_.prepare(preview.job);
  if (Accepted(reply)) settings_ = std::move(settings);
  return reply;
}
ObjectReply LiveFrontend::Enqueue(Turn turn) {
  if (queue_.size() >= kQueuedTurns ||
      turn.charge > kQueuedTurns * kSnapshotBytes - queued_bytes_)
    return Capacity();
  const size_t charge = turn.charge;
  queue_.push_back(std::move(turn));
  queued_bytes_ += charge;
  return {};
}
ObjectReply LiveFrontend::Respond(std::string client_event) {
  if (!Active()) return Unavailable();
  if (queue_.size() >= kQueuedTurns) return Capacity();
  Turn turn;
  turn.client_event = std::move(client_event);
  auto reply = Capture(&turn, settings_);
  return Accepted(reply) ? Enqueue(std::move(turn)) : reply;
}
ObjectReply LiveFrontend::Create(Json item, bool respond,
                                 std::string client_event) {
  if (!Active()) return Unavailable();
  if (respond && queue_.size() >= kQueuedTurns) return Capacity();
  if (client_event.size() > 256) return Invalid("Client event ID too long");
  RealtimeItemTicket ticket;
  const auto generated = detail::NewRequestId("item_");
  // The caller supplies content; native provenance and IDs are never imported.
  Set(&item, "id", String(generated));
  auto status = history_.Create(item, generated, {}, &ticket);
  if (!status) return Invalid(status.message());
  if (!respond) return {};
  auto reply = Respond(std::move(client_event));
  if (!Accepted(reply)) {
    status = history_.Erase(ticket);
    if (!status) {
      reply.commit_unknown = true;
      return Fail(reply);
    }
  }
  return reply;
}
ObjectReply LiveFrontend::Committed(RealtimeInputCommit notice) {
  if (!Active() || !notice.settings.RespondOnCommit(notice.automatic))
    return {};
  if (!Accepted(notice.context_status)) return notice.context_status;
  if (!notice.response)
    return Invalid("Native input lost its response capture");
  Turn turn;
  turn.job.response_id = detail::NewRequestId("resp_");
  turn.job.settings = std::move(notice.response->settings);
  turn.job.response = Object();
  turn.job.snapshot = std::move(notice.context);
  turn.job.native_input = true;
  auto reply = Charge(&turn);
  return Accepted(reply) ? Enqueue(std::move(turn)) : reply;
}
ObjectReply LiveFrontend::AppendAudio(std::string_view audio,
                                      std::string_view client_event) {
  return Active() ? input_.Append(audio, client_event) : Unavailable();
}
ObjectReply LiveFrontend::Mute(bool muted, std::string_view client_event) {
  return Active() ? input_.Mute(muted, client_event) : Unavailable();
}
ObjectReply LiveFrontend::Item(const Json& item, bool begin, bool terminal) {
  if (!active_) return Unavailable();
  const auto id = item.GetString("id");
  const auto previous = history_.LastId();
  q4t::Status status;
  if (begin) {
    RealtimeItemTicket ticket;
    // Maximum output text/tool bytes plus JSON ownership overhead. Actual
    // finalized items release unused reservation through the same ledger.
    status = history_.BeginOutput(item, id, true, 2ULL << 20, &ticket);
    if (status) outputs_.emplace(id, std::move(ticket));
  } else {
    const auto found = outputs_.find(id);
    if (found == outputs_.end()) return Unavailable();
    status = history_.Update(found->second, item, terminal);
  }
  if (!status) return Error(503, "live_history_failed", status.message());
  ObjectReply reply;
  reply.value = Object({{"previous_item_id", String(previous)}});
  return reply;
}
ObjectReply LiveFrontend::Audio(std::string_view id, std::string_view,
                                RealtimeAudioCommit) {
  // Speech bytes were generated and checked by the original worker/verifier.
  // Frontend context retains their transcript, not an unnecessary PCM copy.
  // Optional Live recording has its separate session media owner.
  if (!active_) return Unavailable();
  const auto found = outputs_.find(std::string(id));
  if (found == outputs_.end()) return Unavailable();
  Json item;
  return history_.Get(found->second, &item) ? ObjectReply{} : Unavailable();
}
ObjectReply LiveFrontend::StartWork() {
  if (work_ || queue_.empty() || closing_ || input_.Executing() ||
      input_.PendingItems())
    return {};
  auto& next = queue_.front();
  bool ready = false;
  const auto status = history_.Resolve(&next.job.snapshot, &ready);
  if (status && !ready) return {};
  const size_t charge = next.charge;
  auto turn = std::move(next);
  queue_.pop_front();
  queued_bytes_ -= charge;
  if (!status) return Report(Invalid(status.message()), turn.client_event);
  active_.emplace(std::move(turn));
  LiveFrontendCallbacks callbacks;
  callbacks.prepare = native_.prepare;
  callbacks.acquire = native_.acquire;
  callbacks.release = native_.release;
  callbacks.execute = native_.execute;
  callbacks.item = [this](const auto& item, bool begin, bool terminal) {
    return Item(item, begin, terminal);
  };
  callbacks.audio = [this](auto id, auto bytes, auto commit) {
    return Audio(id, bytes, commit);
  };
  callbacks.emit = native_.emit;
  callbacks.timeline = [this] { return Timeline(); };
  work_ = std::make_unique<LiveFrontendWork>(
      active_->job, id_, format_, std::move(callbacks), active_->client_event);
  const auto reply = work_->Start();
  if (Accepted(reply)) return {};
  work_.reset();
  auto failed = std::move(*active_);
  active_.reset();
  return Report(reply, failed.client_event);
}
ObjectReply LiveFrontend::AdvanceWork() {
  if (!work_) return {};
  auto reply = work_->Tick();
  if (!Accepted(reply)) return reply;
  if (!work_->Finished()) return {};
  // Native work has joined/released. Clear scheduling ownership before the
  // backend receives its one verified result; no nested model execution here.
  auto work = std::move(work_);
  auto turn = std::move(*active_);
  active_.reset();
  outputs_.clear();
  output_ticks_ = std::max(output_ticks_, work->AudioEndTicks());
  const auto& result = work->Result();
  if (Accepted(result.reply) &&
      result.state == RealtimeResponseEnd::State::kCompleted &&
      result.retained_input_indices && turn.job.snapshot.default_context) {
    const auto status = history_.TruncateDefault(
        turn.job.snapshot, *result.retained_input_indices);
    if (!status) return Invalid(status.message());
  }
  reply = native_.completed(turn.job, result, work->Output());
  if (!Accepted(reply)) return reply;
  if (!Accepted(result.reply) &&
      result.state != RealtimeResponseEnd::State::kCancelled)
    return Report(result.reply, turn.client_event);
  return {};
}
ObjectReply LiveFrontend::Report(const ObjectReply& reply,
                                 std::string_view client_event) {
  auto error = Object(
      {{"type", String(reply.http_status >= 500 ? "server_error"
                                                : "invalid_request_error")},
       {"code", String(std::string(reply.ClientCode()))},
       {"message", String(reply.ClientMessage())}});
  if (!client_event.empty())
    Set(&error, "client_event_id", String(std::string(client_event)));
  return native_.emit(
      Object({{"type", String("error")}, {"error", std::move(error)}}));
}
ObjectReply LiveFrontend::Fail(const ObjectReply& reply) {
  failed_ = true;
  if (work_) work_->Cancel();
  input_.Cancel();
  DropQueued();
  return reply;
}
ObjectReply LiveFrontend::Tick() {
  if (!prepared_ || cancelled_ || failed_) return Unavailable();
  try {
    auto reply = AdvanceWork();
    if (Accepted(reply)) reply = input_.Tick();
    if (Accepted(reply)) reply = StartWork();
    return Accepted(reply) ? ObjectReply{} : Fail(reply);
  } catch (...) {
    return Fail(Error(503, "live_frontend_failed", "Frontend callback failed"));
  }
}
void LiveFrontend::DropQueued() noexcept {
  queue_.clear();
  queued_bytes_ = 0;
}
ObjectReply LiveFrontend::Close() {
  if (!prepared_ || cancelled_ || failed_) return Unavailable();
  if (closing_) return {};
  const auto reply = input_.Close();
  if (!Accepted(reply)) return reply;
  closing_ = true;
  DropQueued();
  return {};
}
bool LiveFrontend::Drained() const {
  return prepared_ && closing_ && !cancelled_ && !failed_ && !work_ &&
         queue_.empty() && input_.Drained();
}
void LiveFrontend::Cancel() noexcept {
  cancelled_ = true;
  if (work_) work_->Cancel();
  input_.Cancel();
  DropQueued();
}
void LiveFrontend::Join() noexcept {
  if (joined_) return;
  if (work_) work_->Join();
  input_.Join();
  joined_ = true;
}
}  // namespace q4t::server
