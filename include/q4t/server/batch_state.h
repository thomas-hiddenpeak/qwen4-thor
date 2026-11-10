// Native durable Batch metadata and validation checkpoints. All access scopes
// come from authenticated server context, never from body metadata.
#pragma once

#include <span>

#include "q4t/server/batch_input.h"
#include "q4t/server/binary_store.h"
#include "q4t/server/input_snapshot.h"

namespace q4t::server {
inline constexpr size_t kBatchPageRows = 128;
inline constexpr size_t kBatchPages =
    (kBatchRequests + kBatchPageRows - 1) / kBatchPageRows;

struct BatchRowPosition {
  size_t line = 0;
  uint64_t offset = 0, wire_bytes = 0;
  std::string custom_id_sha256;
  InputSnapshotRef prepared_input = {};
  std::string prepared_binding_sha256 = {};
};
struct BatchRowResult {
  // The durable StartRow ticket, not a client-provided request ID.
  std::string request_id;
  int http_status = 0;
  // Complete finite endpoint JSON including tool output and usage. A failure
  // of orchestration uses error instead and publishes response=null.
  std::string response_body;
  ObjectReply error;
};
bool BatchTerminal(std::string_view status);

class BatchState {
 public:
  explicit BatchState(ObjectStore& objects) : objects_(objects) {}
  // Publishes validating plus an exact private manifest pin atomically with
  // the source read lease. Deleting the public File cannot invalidate it.
  ObjectReply Create(const BinaryAccess& access, const BatchCreate& options);
  ObjectReply Retrieve(const BinaryAccess& access, const std::string& id);
  ObjectReply List(const BinaryAccess& access, const io::Json& query);
  ObjectReply Cancel(const BinaryAccess& access, const std::string& id);

  // Trusted execution API, not public JSON. Load returns private state.
  ObjectReply Load(const BinaryAccess& access, const std::string& id);
  ObjectReply AppendValidationPage(const BinaryAccess& access,
                                   const std::string& id,
                                   std::span<const BatchRowPosition> rows);
  ObjectReply CompleteValidation(const BinaryAccess& access,
                                 const std::string& id,
                                 const std::string& canonical_model);
  ObjectReply FailValidation(const BinaryAccess& access, const std::string& id,
                             const ObjectReply& error);
  // Claim only after independently confirming the previous process identity
  // is gone. Unknown /proc or a live owner must never grant execution.
  ObjectReply ClaimAbandoned(const BinaryAccess& access, const std::string& id);
  ObjectReply ResetValidation(const BinaryAccess& access,
                              const std::string& id);
  ObjectReply FinishCancelled(const BinaryAccess& access,
                              const std::string& id);

  // Zero-based. Reading never grants execution: only a successful StartRow
  // receipt permits one invocation; running/unknown rows must never replay.
  ObjectReply Row(const BinaryAccess& access, const std::string& id,
                  size_t index);
  ObjectReply ReadInputRow(const BinaryAccess& access, const std::string& id,
                           size_t index, BatchInputRow* row);
  ObjectReply StartRow(const BinaryAccess& access, const std::string& id,
                       size_t index);
  ObjectReply FinishRow(const BinaryAccess& access, const std::string& id,
                        size_t index, const std::string& custom_id,
                        const BatchRowResult& result);
  ObjectReply BeginFinalization(const BinaryAccess& access,
                                const std::string& id,
                                const std::string& terminal_status);
  // Stream results into success/error Files; both files and terminal Batch
  // publish in one commit. Partial files are never publicly visible.
  ObjectReply Finalize(const BinaryAccess& access, const std::string& id);

 private:
  ObjectReply Owned(const BinaryAccess& access, const std::string& id);
  ObjectReply Page(const BinaryAccess& access, const io::Json& stored,
                   size_t index);
  ObjectStore& objects_;
};
}  // namespace q4t::server
