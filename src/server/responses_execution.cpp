#include "q4t/server/responses_execution.h"

#include <algorithm>
#include <cstring>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply OutputError(const Status& status) {
  return Error(503, "response_storage_exhausted", status.message());
}
}  // namespace

ObjectReply ResponsesExecution::BindMutationResult(ObjectReply reply) {
  if (reply.commit_unknown) {
    commit_unknown_ = true;
    reply.message +=
        "; retrieve response " + plan_.id + " before retrying this request";
    reply.value = Object({{"response_id", String(plan_.id)}});
  }
  return reply;
}

ObjectReply ResponsesExecution::Prepare(const Json& request,
                                        const ChatPlanConfig& config,
                                        StateAccess access, int64_t created) {
  if (prepared_ || begun_)
    return Error(500, "response_state_invalid", "Response already prepared");
  access_ = std::move(access);
  ChatPlanError error;
  auto status = ParseResponsesEnvelope(request, config, &envelope_, &error);
  if (!status) {
    auto reply = Error(error.http_status, error.code, status.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  // Required application executors remain explicit work in A07--A11. Until
  // attached, do not silently run a different foreground/uncompressed task.
  std::string required;
  if (envelope_.background)
    required = "background";
  else if (envelope_.truncate)
    required = "truncation";
  else if (!envelope_.summary.empty())
    required = "reasoning.summary";
  else if (envelope_.encrypted_reasoning)
    required = "reasoning.encrypted_content";
  else if (envelope_.reasoning_mode != "standard")
    required = "reasoning.mode";
  for (const char* key :
       {"prompt", "access_programs", "moderation", "prompt_cache_options"})
    if (required.empty() && Nonnull(envelope_.request, key)) required = key;
  if (const auto* context = envelope_.request.GetArray("context_management");
      required.empty() && context && !context->array.empty())
    required = "context_management";
  if (!required.empty()) {
    auto reply =
        Error(503, "response_executor_unavailable",
              "Required response executor is unavailable: " + required);
    reply.value = Object({{"param", String(required)}});
    return reply;
  }
  auto resolved = state_.Resolve(envelope_, access_, &plan_);
  if (!resolved.ok()) return resolved;
  status = PrepareResponsesGeneration(envelope_, plan_.history, config,
                                      &generation_, &error);
  if (!status) {
    auto reply = Error(error.http_status, error.code, status.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  Set(&envelope_.options, "tools", generation_.effective_tools);
  // The local scheduler reports the tier actually used.
  Set(&envelope_.options, "service_tier", String("default"));
  status =
      output_.Init(envelope_.options, plan_.id, created, envelope_.obfuscation);
  if (!status) return OutputError(status);
  events_ = Array();
  prepared_ = true;
  return {};
}

Status ResponsesExecution::Capture(std::string_view wire, Json* events,
                                   size_t* bytes) const {
  if (!plan_.store || wire.empty()) return {};
  // Retain actual event objects, never reconstructed snapshots. Both the
  // journal and the final object are additionally bounded by store quotas.
  constexpr size_t limit = 16 * 1024 * 1024;
  if (wire.size() > limit || *bytes > limit - wire.size())
    return Status::Fail("Responses event journal capacity exhausted");
  const size_t appended_bytes = wire.size();
  Json added = Array();
  while (!wire.empty()) {
    const auto line = wire.find("\r\n");
    const auto end = wire.find("\r\n\r\n");
    if (!wire.starts_with("event: ") || line == wire.npos || end == wire.npos ||
        end <= line + 8 || wire.substr(line + 2, 6) != "data: ")
      return Status::Fail("Invalid native Responses event framing");
    const auto type = wire.substr(7, line - 7);
    Json value;
    auto status =
        io::ParseJson(std::string(wire.substr(line + 8, end - line - 8)),
                      &value, true, kSchemaJsonLimits);
    if (!status || value.GetString("type") != type ||
        value.GetInt("sequence_number", -1) !=
            static_cast<int64_t>(events->array.size() + added.array.size()))
      return Status::Fail("Invalid native Responses event sequence");
    added.array.push_back(std::move(value));
    wire.remove_prefix(end + 4);
  }
  events->array.insert(events->array.end(),
                       std::make_move_iterator(added.array.begin()),
                       std::make_move_iterator(added.array.end()));
  *bytes += appended_bytes;
  return {};
}

ObjectReply ResponsesExecution::Begin(std::string* wire) {
  wire->clear();
  if (!prepared_ || begun_ || commit_unknown_)
    return Error(500, "response_state_invalid", "Invalid response begin");
  auto candidate = output_;
  auto events = events_;
  size_t bytes = event_bytes_;
  std::string start;
  auto status = candidate.Start(&start);
  if (status) status = Capture(start, &events, &bytes);
  if (!status) return OutputError(status);
  auto reply = BindMutationResult(
      state_.Begin(candidate.Snapshot(), events, access_, &plan_));
  if (!reply.ok()) return reply;
  output_ = std::move(candidate);
  events_ = std::move(events);
  event_bytes_ = checkpoint_bytes_ = bytes;
  checkpoint_time_ = std::chrono::steady_clock::now();
  begun_ = true;
  *wire = std::move(start);
  return {};
}

ObjectReply ResponsesExecution::Checkpoint() {
  if (!begun_ || plan_.finished || commit_unknown_)
    return Error(500, "response_state_invalid", "Invalid response checkpoint");
  auto reply = BindMutationResult(
      state_.Checkpoint(output_.Snapshot(), events_, access_, &plan_));
  if (reply.ok()) {
    checkpoint_time_ = std::chrono::steady_clock::now();
    checkpoint_bytes_ = event_bytes_;
  }
  return reply;
}

Status ResponsesExecution::Append(const ResponseDelta& delta,
                                  std::string_view probabilities,
                                  std::string* wire) {
  auto status = output_.Append(delta, probabilities, wire);
  if (status) status = Capture(*wire, &events_, &event_bytes_);
  if (status && plan_.store &&
      (event_bytes_ - checkpoint_bytes_ >= 64 * 1024 ||
       std::chrono::steady_clock::now() - checkpoint_time_ >=
           std::chrono::milliseconds(500))) {
    auto saved = Checkpoint();
    if (!saved.ok()) {
      ObserveError(this, saved.http_status, saved.message, saved.code);
      return Status::Fail(saved.message);
    }
  }
  return status;
}

ObjectReply ResponsesExecution::Finish(bool incomplete, size_t completed_calls,
                                       const ResponsesUsage& usage,
                                       int64_t completed_at,
                                       std::string* wire) {
  wire->clear();
  if (!begun_ || plan_.finished || commit_unknown_)
    return Error(500, "response_state_invalid", "Invalid response finish");
  // Work on a candidate: a failed transaction must not consume a success
  // sequence number or make the live renderer think it already completed.
  auto candidate = output_;
  auto events = events_;
  size_t bytes = event_bytes_;
  std::string terminal;
  auto status = candidate.Finish(incomplete, completed_calls, usage,
                                 completed_at, &terminal);
  if (status) status = Capture(terminal, &events, &bytes);
  if (!status) return OutputError(status);
  auto result = BindMutationResult(
      state_.Finish(candidate.Snapshot(), events, access_, &plan_));
  if (!result.ok()) return result;
  output_ = std::move(candidate);
  events_ = std::move(events);
  event_bytes_ = bytes;
  *wire = std::move(terminal);
  return {};
}

void ResponsesExecution::ObserveError(void* owner, int status,
                                      std::string_view message,
                                      std::string_view code) noexcept {
  auto& run = *static_cast<ResponsesExecution*>(owner);
  if (run.failure_status_) return;
  run.failure_status_ = status;
  const auto save = [](auto& destination, std::string_view source) {
    const auto size = std::min(destination.size() - 1, source.size());
    std::memcpy(destination.data(), source.data(), size);
    destination[size] = '\0';
  };
  save(run.failure_code_, code.empty() ? "response_execution_failed" : code);
  save(run.failure_message_, message);
}

std::string ResponsesExecution::RenderStreamError(void* owner, int status,
                                                  const std::string& message,
                                                  std::string_view code,
                                                  std::string_view param) {
  auto& run = *static_cast<ResponsesExecution*>(owner);
  ObserveError(owner, status, message, code);
  if (run.commit_unknown_) {
    // The durable decision is unknown; do not falsely advertise failed when
    // retrieval might find a completed transaction.
    const auto event = Object({{"type", String("error")},
                               {"sequence_number", Number((*run.Sequence())++)},
                               {"code", String("object_commit_unknown")},
                               {"message", String(message)},
                               {"param", Json()}});
    return "event: error\r\ndata: " + OutputJson(event) + "\r\n\r\n";
  }
  auto wire = ResponsesOutput::RenderStreamError(&run.output_, status, message,
                                                 code, param);
  // A failed response can still be retrieved when its event journal exceeded
  // its resource budget; no invented events are substituted for the originals.
  if (!run.Capture(wire, &run.events_, &run.event_bytes_))
    run.events_ = Array();
  return wire;
}

ObjectReply ResponsesExecution::CloseUnfinished() {
  if (!begun_ || plan_.finished || commit_unknown_) return {};
  if (!output_.Terminal()) {
    const auto code = failure_status_ ? failure_code_.data()
                                      : "response_execution_interrupted";
    const auto message = failure_status_
                             ? detail::NormalizeUtf8(failure_message_.data())
                             : "Response execution ended before completion";
    std::string wire;
    auto status = output_.Fail(code, message, &wire);
    if (!status) return OutputError(status);
    if (!Capture(wire, &events_, &event_bytes_)) events_ = Array();
  }
  StateAccess cleanup{
      access_.scope,
      std::chrono::steady_clock::now() + std::chrono::milliseconds(2000),
      {}};
  auto saved = BindMutationResult(
      state_.Finish(output_.Snapshot(), events_, cleanup, &plan_));
  if (!saved.commit_unknown && (saved.code == "object_too_large" ||
                                saved.code == "object_quota_exceeded"))
    return BindMutationResult(
        state_.FailWithinReservation(output_.Snapshot(), cleanup, &plan_));
  return saved;
}

}  // namespace q4t::server
