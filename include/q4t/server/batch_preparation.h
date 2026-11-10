// Private endpoint preparation before any Batch row may invoke a model.
#pragma once

#include "q4t/server/batch_state.h"
#include "q4t/server/chat_plan.h"

namespace q4t::server {
struct BatchPreparedInput {
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  std::string binding_sha256, canonical_model;
  io::Json inline_input = {};
};

class BatchPreparation {
 public:
  explicit BatchPreparation(ObjectStore& objects) : objects_(objects) {}
  // Resolves authorized history/prompts/files and validates the actual native
  // planner, but publishes no public Response/Chat and invokes no model.
  // The resulting immutable root must be retained in a validation page.
  ObjectReply Prepare(const BinaryAccess& access, const std::string& batch_id,
                      std::string_view endpoint, const BatchInputRow& row,
                      const ChatPlanConfig& config, BatchPreparedInput* output);
  ObjectReply Read(const BinaryAccess& access, const std::string& batch_id,
                   std::string_view endpoint, const BatchInputRow& row,
                   const io::Json& checkpoint, io::Json* prepared);
  void Published(const BinaryAccess& access, BatchPreparedInput* input,
                 bool uncertain) noexcept;

 private:
  ObjectStore& objects_;
};
}  // namespace q4t::server
