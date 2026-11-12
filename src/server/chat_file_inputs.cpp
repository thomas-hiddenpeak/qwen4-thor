#include "q4t/server/chat_file_inputs.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/input_snapshot_bindings.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(const Status& status, const ChatPlanError& error) {
  auto reply = Error(error.http_status, error.code, status.message());
  reply.value = Object({{"param", String(error.param)}});
  return reply;
}
std::string PrivateId(size_t index) {
  return "chatinput_" + std::to_string(index);
}
}  // namespace

std::string StoredChatMessageId(const std::string& id, size_t index) {
  return "chatmsg_" + Digest(id + ':' + std::to_string(index));
}

ObjectReply ChatFileInputs::Prepare(const Json& request,
                                    const ChatPlanConfig& config,
                                    const BinaryAccess& access,
                                    ChatRequestPlan* plan,
                                    const Json& bindings,
                                    bool require_frozen) {
  if (files_)
    return Error(500, "file_input_state_invalid", "Inputs already prepared");
  auto validation = config;
  validation.preparing_file_inputs = true;
  ChatPlanError error;
  auto status = PlanChatRequest(request, validation, plan, &error);
  if (!status) return Invalid(status, error);
  const auto& normalized = plan->Request(request);
  const auto* messages = normalized.GetArray("messages");
  if (!messages || !HasFileInputs(*messages)) return {};
  original_items_ = *messages;
  for (size_t i = 0; i < original_items_.array.size(); ++i)
    Set(&original_items_.array[i], "id", String(PrivateId(i)));
  files_ = std::make_unique<FileExpansion>(
      objects_,
      config.file_inputs ? *config.file_inputs : FileExpansionOptions{});
  auto reply = files_->Prepare(original_items_, FileInputProfile::kChat,
                               bindings, access, require_frozen);
  if (!reply.ok()) return reply;
  Json expanded;
  reply = files_->Render(original_items_, &expanded);
  if (!reply.ok()) return reply;
  for (auto& item : expanded.array)
    std::erase_if(item.object,
                  [](const auto& field) { return field.first == "id"; });
  auto model_request = normalized;
  Set(&model_request, "messages", std::move(expanded));
  const bool legacy = plan->legacy_output;
  validation.preparing_file_inputs = false;
  status = PlanChatRequest(model_request, validation, plan, &error);
  if (!status) return Invalid(status, error);
  plan->legacy_output = legacy;
  plan->normalized_request = std::move(model_request);
  return {};
}

ObjectReply ChatFileInputs::Freeze(const BinaryAccess& access,
                                   Json* bindings) {
  if (!bindings)
    return Error(500, "file_input_state_invalid", "Missing private bindings");
  *bindings = Array();
  if (!files_) return {};
  auto reply = files_->Persist(access);
  if (reply.ok()) *bindings = files_->Bindings();
  return reply;
}

ObjectReply ChatFileInputs::Persist(const std::string& id,
                                    const BinaryAccess& access,
                                    Json* bindings) {
  *bindings = Array();
  if (!files_) return {};
  if (!ValidId(id))
    return Error(500, "file_input_state_invalid", "Invalid completion ID");
  auto reply = files_->Persist(access);
  if (!reply.ok()) return reply;
  auto remapped = files_->Bindings();
  auto items = original_items_;
  for (size_t i = 0; i < items.array.size(); ++i) {
    const auto stored_id = StoredChatMessageId(id, i);
    Set(&items.array[i], "id", String(stored_id));
    for (auto& binding : remapped.array)
      if (binding.GetString("item_id") == PrivateId(i))
        Set(&binding, "item_id", String(stored_id));
  }
  return MergeInputSnapshots(remapped, items, bindings);
}

void ChatFileInputs::Published(const BinaryAccess& access,
                               bool uncertain) noexcept {
  if (!files_) return;
  if (uncertain) {
    files_->RetainUncertainHolds();
    return;
  }
  try {
    // Publication already owns the references. Auxiliary hold cleanup cannot
    // turn a durable completion into an error, even after client disconnect.
    files_->Release(
        {access.scope,
         std::chrono::steady_clock::now() + std::chrono::milliseconds(250),
         {}});
  } catch (...) {
    // Absolute holds expire; no speculative retry of the parent publication.
  }
}
}  // namespace q4t::server
