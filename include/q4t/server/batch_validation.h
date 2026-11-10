// Incremental native validation. One completed row per scheduling turn; all
// rows must pass the original JSONL contract before any model may execute.
#pragma once

#include <memory>
#include <optional>

#include "q4t/server/batch_preparation.h"

namespace q4t::server {
class BatchValidation {
 public:
  BatchValidation(ObjectStore& objects, std::string id, ChatPlanConfig config)
      : objects_(objects), batches_(objects), preparation_(objects),
        id_(std::move(id)), config_(std::move(config)) {}
  ~BatchValidation();
  BatchValidation(const BatchValidation&) = delete;
  BatchValidation& operator=(const BatchValidation&) = delete;
  // Reads only the original pinned Batch input. Previously prepared rows are
  // checked and reused, never fetched or prepared again. Errors are sticky;
  // an owner reconciles durable state before constructing another driver.
  ObjectReply Step(BinaryAccess access);
  bool Complete() const { return complete_; }
  size_t RowsChecked() const { return input_ ? input_->Rows() : 0; }
  uint64_t BytesChecked() const { return input_ ? input_->Bytes() : 0; }
  const ObjectReply& Result() const { return result_; }

 private:
  ObjectReply Initialize(const io::Json& data);
  ObjectReply Check(const BatchInputRow& row, BatchRowCheck* checked);
  ObjectReply Stage(BatchInputRow&& row);
  ObjectReply Fill();
  ObjectReply Finish();
  ObjectReply Fail(ObjectReply reply);
  void ReleasePrepared(bool uncertain) noexcept;
  ObjectStore& objects_;
  BatchState batches_;
  BatchPreparation preparation_;
  std::string id_, scope_, endpoint_, model_, chunk_;
  ChatPlanConfig config_;
  BinaryAccess access_;
  BinaryManifest source_;
  std::unique_ptr<BatchInput> input_;
  std::optional<BatchPreparedInput> prepared_;
  size_t frozen_rows_ = 0, next_chunk_ = 0, chunk_at_ = 0;
  int64_t execution_deadline_at_ = 0;
  bool complete_ = false;
  ObjectReply result_;
};
}  // namespace q4t::server
