#include "q4t/server/realtime_file_inputs.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/realtime_conversation.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid() {
  return Error(500, "realtime_input_owner_invalid",
               "Realtime file preparation ownership changed");
}
ObjectReply Active(const BinaryAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled",
                 "Realtime file preparation cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Realtime file preparation expired");
  return {};
}
ObjectReply Failure(const Status& status, const ChatPlanError& error) {
  auto reply = Error(error.http_status, error.code, status.message());
  reply.value = Object({{"param", String(error.param)}});
  return reply;
}
std::string Identity(const RealtimeChatPlan& plan) {
  return Digest(OutputJson(plan.envelope.request) + '\n' +
                OutputJson(plan.truncation));
}
}  // namespace

ObjectReply RealtimeFileInputs::Prepare(const RealtimeChatPlan& plan,
                                        const ChatPlanConfig& config,
                                        ObjectStore* objects,
                                        BinaryAccess access) {
  if (attempted_) return Invalid();
  attempted_ = true;
  if (plan.input_ready || access.scope.empty()) return Invalid();
  const auto* original = plan.envelope.request.GetArray("input");
  if (!original || !HasFileInputs(*original)) return Invalid();
  auto reply = Active(access);
  if (!reply.ok()) return reply;
  Json tools;
  ChatPlanError error;
  const auto status = ValidateResponsesPreparation(plan.envelope, *original,
                                                   config, &tools, &error);
  if (!status) return Failure(status, error);
  auto items = *original;
  for (size_t i = 0; i < items.array.size(); ++i)
    Set(&items.array[i], "id", String("rtinput_" + std::to_string(i)));
  size_t bytes = 0;
  if (!MeasureRealtimeStorage(items, 16ULL << 20, &bytes))
    return Error(413, "realtime_input_capacity",
                 "Realtime file input exceeds its retained byte limit");
  auto options =
      config.file_inputs ? *config.file_inputs : FileExpansionOptions{};
  // Part of the existing per-worker protocol reservation. Preserve stricter
  // deployment limits; the ordinary codec/model memory admissions still apply.
  options.max_source_bytes =
      std::min<uint64_t>(options.max_source_bytes, 32ULL << 20);
  options.max_retained_bytes =
      std::min<uint64_t>(options.max_retained_bytes, 64ULL << 20);
  options.max_rendered_bytes =
      std::min<uint64_t>(options.max_rendered_bytes, 16ULL << 20);
  auto files = std::make_unique<FileExpansion>(objects, std::move(options));
  reply = files->Prepare(items, FileInputProfile::kResponsesStored, Array(),
                         access);
  if (!reply.ok() || reply.commit_unknown) return reply;
  reply = Active(access);
  if (!reply.ok()) return reply;
  request_sha256_ = Identity(plan);
  prompt_items_ = plan.prompt_items;
  private_items_ = std::move(items);
  access_ = std::move(access);
  files_ = std::move(files);
  return {};
}

ObjectReply RealtimeFileInputs::Finish(const ChatPlanConfig& config,
                                       RealtimeChatPlan* plan) {
  if (!plan || !files_ || completed_ || plan->input_ready ||
      plan->prompt_items != prompt_items_ || Identity(*plan) != request_sha256_)
    return Invalid();
  auto reply = Active(access_);
  if (!reply.ok()) return reply;
  Json expanded;
  reply = files_->Render(private_items_, &expanded);
  if (!reply.ok() || reply.commit_unknown) return reply;
  auto candidate = *plan;
  ChatPlanError error;
  const auto status =
      CompleteRealtimeChatInput(config, expanded, true, &candidate, &error);
  if (!status) return Failure(status, error);
  reply = Active(access_);
  if (!reply.ok()) return reply;
  *plan = std::move(candidate);
  completed_ = true;
  return {};
}
}  // namespace q4t::server
