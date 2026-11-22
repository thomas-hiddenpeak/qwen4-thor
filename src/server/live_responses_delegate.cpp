#include "q4t/server/live_responses_delegate.h"

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/responses_item_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
constexpr size_t kHistory = 8ULL << 20, kItems = 256;
constexpr size_t kMailbox = 8ULL << 20, kEvents = 256;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Capacity() {
  return Error(429, "live_response_capacity",
               "Live backend history capacity exhausted");
}
bool ToolResult(std::string_view type) {
  return type.ends_with("_call_output") || type == "mcp_approval_response" ||
         type == "tool_search_output";
}
bool Fits(const Json& input) {
  size_t bytes = 0;
  return input.IsArray() && input.array.size() <= kItems &&
         MeasureRealtimeStorage(input, kHistory, &bytes).ok();
}
}  // namespace
LiveResponsesDelegate::LiveResponsesDelegate(
    LiveResponsesNative native, LiveEventSink emit,
    std::function<ObjectReply(std::string)> speak)
    : native_(std::move(native)),
      emit_(std::move(emit)),
      speak_(std::move(speak)),
      history_(Array()),
      added_(Array()) {}
LiveResponsesDelegate::~LiveResponsesDelegate() {
  Close();
  Interrupt();
  Join();
}
ObjectReply LiveResponsesDelegate::Add(const Json& item) {
  if (closing_) return Error(409, "session_closing", "Live session is closing");
  if (ToolResult(item.GetString("type"))) {
    return turn_ ? turn_->Result(item)
                 : Error(400, "invalid_tool_output",
                         "No matching delegated call");
  }
  size_t charge = 0;
  if (!MeasureRealtimeStorage(item, kHistory, &charge) ||
      added_.array.size() >= kItems)
    return Capacity();
  ChatPlanError error;
  auto status = ValidateResponsesItemShape(item, ResponsesItemShape::kInput,
                                           &error, "item");
  if (!status) return Error(error.http_status, error.code, status.message());
  // Application input has no authority to register calls; only the trusted
  // stream's output_item.done can establish a pending tool result.
  auto candidate = added_;
  candidate.array.push_back(item);
  if (!Fits(candidate)) return Capacity();
  added_ = std::move(candidate);
  return {};
}
ObjectReply LiveResponsesDelegate::Create(std::string event) {
  if (closing_ || !ready_)
    return Error(409, "responses_settings_unavailable",
                 "Responses settings are unavailable");
  if (turn_) {
    auto reply = turn_->RequestContinuation(std::move(event));
    return Accepted(reply) ? Next() : reply;
  }
  return Start(detail::NewRequestId("delegation_"), std::move(event), {});
}
ObjectReply LiveResponsesDelegate::Delegate(std::string id, Json task) {
  if (closing_ || !ready_)
    return Error(409, "responses_settings_unavailable",
                 "Responses settings are unavailable");
  if (Running() || (turn_ && turn_->HasCalls()))
    return Error(409, "response_in_progress",
                 "A delegated response is still active");
  return Start(std::move(id), {}, std::move(task));
}
ObjectReply LiveResponsesDelegate::Start(std::string id, std::string event,
                                         Json task) {
  if (!native_.acquire || !native_.release || !native_.execute || !emit_ ||
      !speak_ || !ValidApplicationId(id) || Running())
    return Error(503, "live_response_unavailable",
                 "Responses executor unavailable");
  auto input = history_;
  if (turn_) {
    auto reply = turn_->AppendHistory(&input);
    if (!Accepted(reply)) return reply;
  }
  input.array.insert(input.array.end(), added_.array.begin(),
                     added_.array.end());
  if (!task.IsNull()) input.array.push_back(std::move(task));
  if (!Fits(input)) return Capacity();
  auto next = std::make_unique<LiveResponsesTurn>(std::move(id), event, emit_);
  auto request = detail::NewRequestId("req_");
  control_ = native_.acquire(request);
  if (!control_ || control_->id() != request) {
    Release();
    return Error(503, "live_response_owner_invalid",
                 "Responses request registration failed");
  }
  try {
    worker_ = std::async(
        std::launch::async, [this, settings = settings_, history = input,
                             control = control_]() mutable {
          return native_.execute(settings, std::move(history), control,
                                 [this, control](const auto& value) {
                                   return Enqueue(value, control);
                                 });
        });
  } catch (...) {
    Release();
    return Error(503, "live_response_start_failed",
                 "Could not start Responses worker");
  }
  history_ = std::move(input);
  added_.array.clear();
  command_ = std::move(event);
  turn_ = std::move(next);
  interrupted_ = false;
  return {};
}
ObjectReply LiveResponsesDelegate::Enqueue(
    const Json& event, const std::shared_ptr<RequestControl>& control) {
  size_t charge = 0;
  if (!MeasureRealtimeStorage(event, kMailbox, &charge)) return Capacity();
  const auto deadline =
      std::min(control->deadline(), std::chrono::steady_clock::now() + 15s);
  std::unique_lock lock(mutex_);
  while (mailbox_.size() >= kEvents || charge > kMailbox - bytes_) {
    if (control->Cancelled() || std::chrono::steady_clock::now() >= deadline)
      return Error(503, "live_response_backpressure",
                   "Live backend output stalled");
    changed_.wait_for(lock, 5ms);
  }
  if (control->Cancelled())
    return Error(409, control->Reason(), "Delegated response cancelled");
  mailbox_.push_back({event, charge});
  bytes_ += charge;
  return {};
}
ObjectReply LiveResponsesDelegate::Report(ObjectReply reply) {
  if (reply.commit_unknown) return reply;
  auto details = Object(
      {{"type", String(reply.http_status >= 500 ? "server_error"
                                                : "invalid_request_error")},
       {"code", String(reply.code)},
       {"message", String(reply.message)}});
  auto event = Object({{"type", String("error")}});
  if (!command_.empty()) {
    Set(&event, "client_event_id", String(command_));
    Set(&details, "client_event_id", String(command_));
  }
  if (const auto param = reply.value.GetString("param"); !param.empty())
    Set(&details, "param", String(param));
  Set(&event, "error", std::move(details));
  return emit_(std::move(event));
}
ObjectReply LiveResponsesDelegate::Next() {
  if (!turn_ || Running() || closing_) return {};
  auto next = turn_->TakeContinuation();
  if (!next) return {};
  if (!ready_) {
    turn_->RefusedContinuation();
    command_ = *next;
    return Report(Error(409, "responses_settings_unavailable",
                        "Responses settings changed before continuation"));
  }
  command_ = *next;
  auto reply = Start(detail::NewRequestId("delegation_"), std::move(*next), {});
  if (Accepted(reply)) return {};
  turn_->RefusedContinuation();
  return Report(std::move(reply));
}
ObjectReply LiveResponsesDelegate::Tick(uint64_t offset_ms) {
  if (!Running()) return {};
  for (unsigned n = 0; n < 64; ++n) {
    Queued item;
    {
      std::lock_guard lock(mutex_);
      if (mailbox_.empty()) break;
      item = std::move(mailbox_.front());
      mailbox_.pop_front();
      bytes_ -= item.bytes;
    }
    changed_.notify_one();
    if (!interrupted_) {
      auto reply = turn_->Event(item.event, offset_ms);
      if (!Accepted(reply)) {
        Interrupt();
        return reply;
      }
    }
  }
  if (worker_.wait_for(0ms) != std::future_status::ready) return {};
  {
    std::lock_guard lock(mutex_);
    if (!mailbox_.empty()) return {};
  }
  LiveResponsesResult result;
  try {
    result = worker_.get();
  } catch (...) {
    result.reply =
        Error(503, "live_response_execution_failed", "Responses worker failed");
  }
  Release();
  if (interrupted_) {
    turn_.reset();
    return {};
  }
  const auto status = result.terminal.GetString("status");
  if (!Accepted(result.reply) || status != "completed") {
    const bool forwarded = !result.terminal.IsNull();
    turn_.reset();
    if (result.reply.commit_unknown) return result.reply;
    if (forwarded) return {};
    return Report(Accepted(result.reply)
                      ? Error(503, "live_response_incomplete",
                              "Missing Responses terminal")
                      : std::move(result.reply));
  }
  auto reply = turn_->Finish(result.terminal);
  if (!Accepted(reply)) return reply;
  if (closing_) return {};
  if (turn_->HasCalls()) return Next();
  std::string text;
  for (const auto& item : result.terminal.GetArray("output")->array) {
    const auto* parts = item.GetArray("content");
    if (item.GetString("type") != "message" || !parts) continue;
    for (const auto& part : parts->array) {
      const auto type = part.GetString("type");
      if (type == "output_text" || type == "refusal") {
        if (!text.empty()) text += '\n';
        text += part.GetString(type == "refusal" ? "refusal" : "text");
      }
    }
  }
  if (!text.empty()) {
    reply = speak_(std::move(text));
    if (!Accepted(reply)) return Report(std::move(reply));
  }
  return Next();
}
void LiveResponsesDelegate::Release() noexcept {
  if (!control_) return;
  try {
    native_.release(control_);
  } catch (...) {
  }
  control_.reset();
}
void LiveResponsesDelegate::Interrupt() noexcept {
  if (!Running()) return;
  interrupted_ = true;
  if (control_) control_->Cancel();
  if (turn_) turn_->Abort();
  changed_.notify_all();
}
Json LiveResponsesDelegate::ForkHistory() const {
  return Object({{"history", history_},
                 {"added", added_},
                 {"turn", turn_ ? turn_->ForkHistory() : Json{}}});
}
void LiveResponsesDelegate::Close() noexcept { closing_ = true; }
void LiveResponsesDelegate::Join() noexcept {
  if (Running()) {
    Interrupt();
    try {
      worker_.get();
    } catch (...) {
    }
    Release();
  }
  std::lock_guard lock(mutex_);
  mailbox_.clear();
  bytes_ = 0;
}
}  // namespace q4t::server
