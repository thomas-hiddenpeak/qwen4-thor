// Standard Batch envelopes and bounded, incremental JSONL validation.
#pragma once

#include <functional>
#include <optional>
#include <string>
#include <unordered_set>

#include "q4t/server/object_store.h"
#include "q4t/server/batch_response_link.h"
#include "q4t/server/request_limits.h"

namespace q4t::server {
inline constexpr uint64_t kBatchInputBytes = 200000000;
inline constexpr size_t kBatchRequests = 50000;
inline constexpr int64_t kBatchWindowSeconds = 24 * 3600;

struct BatchCreate {
  std::string input_file_id, endpoint;
  io::Json metadata;
  int64_t output_ttl_seconds = 30 * 24 * 3600;
};
bool ValidBatchEndpoint(std::string_view endpoint);
// Transactional: an invalid input does not replace the caller's options.
ObjectReply ParseBatchCreate(const io::Json& request, BatchCreate* result);

struct BatchInputRow {
  size_t line = 0;
  uint64_t offset = 0, wire_bytes = 0;
  std::string custom_id;
  io::Json body;
  // Set only by the native task owner after its durable StartRow receipt.
  std::optional<BatchResponseLink> background_response = {};
};
struct BatchRowCheck {
  // Endpoint validation resolves aliases/defaults and may read authorized
  // history/files into private immutable preparation. It must not execute a
  // model or publish a public Response. Compare model identity after resolving.
  std::string canonical_model;
  size_t embedding_inputs = 0;
};
struct BatchInputLimits {
  uint64_t max_bytes = kBatchInputBytes;
  size_t max_requests = kBatchRequests;
  size_t max_line_bytes = kChatBodyBytes;
};

class BatchInput {
 public:
  using Check =
      std::function<ObjectReply(const BatchInputRow&, BatchRowCheck*)>;
  // May only stage private state. No execution or public acceptance is allowed
  // until Finish succeeds; a later invalid line invalidates the whole file.
  using Stage = std::function<ObjectReply(BatchInputRow&&)>;
  BatchInput(std::string endpoint, Check check, Stage stage,
             BatchInputLimits limits = {},
             std::function<bool()> cancelled = {});
  ObjectReply Feed(std::string_view bytes);
  ObjectReply Finish();
  size_t Rows() const { return rows_; }
  uint64_t Bytes() const { return bytes_; }
  size_t BufferedBytes() const { return buffer_.size(); }
  const std::string& Model() const { return model_; }
  const ObjectReply& Result() const { return result_; }

 private:
  ObjectReply Fail(ObjectReply reply);
  ObjectReply Line(bool newline);
  ObjectReply Interrupted();
  std::string endpoint_, buffer_, model_;
  Check check_;
  Stage stage_;
  BatchInputLimits limits_;
  std::function<bool()> cancelled_;
  // Fixed-size SHA256 identities bound duplicate tracking independently of
  // custom_id length. Original IDs stay exact in each staged row.
  std::unordered_set<std::string> ids_;
  uint64_t bytes_ = 0, offset_ = 0;
  size_t rows_ = 0, embedding_inputs_ = 0;
  bool finished_ = false;
  ObjectReply result_;
};
}  // namespace q4t::server
