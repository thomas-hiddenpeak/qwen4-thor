#include "q4t/server/batch_validation.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_owner.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
ObjectReply Invalid() {
  return Error(503, "batch_validation_state_invalid",
               "Original Batch validation checkpoint is inconsistent");
}
}  // namespace

BatchValidation::~BatchValidation() { ReleasePrepared(false); }

void BatchValidation::ReleasePrepared(bool uncertain) noexcept {
  if (!prepared_) return;
  preparation_.Published(access_, &*prepared_, uncertain);
  prepared_.reset();
}
ObjectReply BatchValidation::Fail(ObjectReply reply) {
  ReleasePrepared(reply.commit_unknown);
  result_ = std::move(reply);
  return result_;
}

ObjectReply BatchValidation::Initialize(const Json& data) {
  const auto* batch = data.Find("batch");
  const auto* blob = data.Find("blob");
  if (!batch || !blob || !ParseBinaryManifest(*blob, &source_))
    return Invalid();
  scope_ = access_.scope;
  endpoint_ = batch->GetString("endpoint");
  model_ = data.GetString("canonical_model");
  frozen_rows_ = data.GetInt("validated_rows");
  execution_deadline_at_ = batch->GetInt("expires_at");
  if (frozen_rows_ && model_.empty()) {
    // Old positions without prepared external inputs may restart validation.
    // Frozen records never use this reset path.
    if (data.GetBool("inputs_frozen"))
      return Error(409, "batch_validation_model_missing",
                   "Frozen validation has no original canonical model");
    auto reset = batches_.ResetValidation(access_, id_);
    if (!reset.ok()) return reset;
    frozen_rows_ = 0;
  }
  input_ = std::make_unique<BatchInput>(
      endpoint_, [this](const auto& row, auto* checked) {
        return Check(row, checked);
      },
      [this](auto&& row) { return Stage(std::move(row)); }, BatchInputLimits{},
      [this] {
        return Clock::now() >= access_.deadline ||
               (access_.cancelled && access_.cancelled());
      });
  return {};
}

ObjectReply BatchValidation::Check(const BatchInputRow& row,
                                    BatchRowCheck* checked) {
  if (row.line <= frozen_rows_) {
    auto checkpoint = batches_.Row(access_, id_, row.line - 1);
    if (!checkpoint.ok()) return checkpoint;
    if (checkpoint.value.GetInt("line") != static_cast<int64_t>(row.line) ||
        checkpoint.value.GetInt("offset") != static_cast<int64_t>(row.offset) ||
        checkpoint.value.GetInt("wire_bytes") !=
            static_cast<int64_t>(row.wire_bytes) ||
        checkpoint.value.GetString("custom_id_sha256") != Digest(row.custom_id))
      return Invalid();
    Json restored;
    auto read = preparation_.Read(access_, id_, endpoint_, row,
                                   checkpoint.value, &restored);
    if (!read.ok()) return read;
    checked->canonical_model = model_;
    return {};
  }
  if (prepared_) return Invalid();
  prepared_.emplace();
  auto result = preparation_.Prepare(access_, id_, endpoint_, row, config_,
                                     &*prepared_, meter_);
  if (result.ok()) checked->canonical_model = prepared_->canonical_model;
  return result;
}

ObjectReply BatchValidation::Stage(BatchInputRow&& row) {
  if (row.line <= frozen_rows_) return {};
  if (!prepared_) return Invalid();
  BatchRowPosition position{row.line,
                            row.offset,
                            row.wire_bytes,
                            Digest(row.custom_id),
                            prepared_->reference,
                            prepared_->binding_sha256,
                            prepared_->inline_input,
                            prepared_->input_budget};
  auto reply = batches_.AppendValidatedRow(access_, id_, position,
                                            prepared_->canonical_model);
  ReleasePrepared(reply.commit_unknown);
  return reply;
}

ObjectReply BatchValidation::Fill() {
  if (next_chunk_ >= source_.chunks.size()) return Invalid();
  BinaryRead read;
  BinaryStore binary(objects_);
  auto result =
      binary.Open(access_, ObjectKind::kBatch, id_, 3600, &read);
  if (!result.ok()) return result;
  ScopeExit close([&] {
    try {
      const BinaryAccess cleanup{
          access_.scope, Clock::now() + std::chrono::milliseconds(250), {}};
      if (!binary.Close(cleanup, &read).ok())
        objects_.RecordAuxiliaryCleanupFailure();
    } catch (...) {
      objects_.RecordAuxiliaryCleanupFailure();
    }
  });
  // A new bounded read pin cannot silently replace the original source.
  // Close it within this turn: fair scheduling may pause longer than a lease.
  if (OutputJson(BinaryManifestJson(read.manifest)) !=
      OutputJson(BinaryManifestJson(source_)))
    return Invalid();
  std::string bytes;
  result = binary.Read(access_, read, next_chunk_, &bytes);
  if (!result.ok()) return result;
  chunk_ = std::move(bytes);
  chunk_at_ = 0;
  ++next_chunk_;
  return {};
}

ObjectReply BatchValidation::Finish() {
  auto result = input_->Finish();
  if (!result.ok()) return Fail(result);
  if (input_->Bytes() != source_.bytes || input_->Rows() < frozen_rows_)
    return Fail(Invalid());
  result = batches_.CompleteValidation(access_, id_, input_->Model());
  if (!result.ok()) return Fail(result);
  complete_ = true;
  result_ = result;
  return result_;
}

ObjectReply BatchValidation::Step(BinaryAccess access) {
  if (access.scope.empty() || (!scope_.empty() && access.scope != scope_))
    return Invalid();
  if (!result_.ok() || complete_) return result_;
  access_ = std::move(access);
  if (access_.cancelled && access_.cancelled())
    return Fail(Error(499, "batch_cancelled", "Batch validation cancelled"));
  if (Clock::now() >= access_.deadline)
    return Fail(Error(408, "request_deadline_exceeded",
                       "Batch validation turn exceeded its deadline"));
  auto current = batches_.Load(access_, id_);
  if (!current.ok()) return Fail(current);
  const auto& data = *current.value.Find("data");
  const auto& batch = *data.Find("batch");
  try {
    const auto* owner = data.Find("owner");
    if (!owner || OutputJson(*owner) != OutputJson(CurrentResponseOwner()))
      return Fail(Error(409, "batch_owner_changed",
                         "Batch validation belongs to another process"));
  } catch (...) {
    return Fail(Error(503, "batch_owner_unavailable",
                       "Batch process identity is unavailable"));
  }
  const auto state = batch.GetString("status");
  if (state == "cancelling" || state == "cancelled")
    return Fail(Error(499, "batch_cancelled", "Batch validation cancelled"));
  if (state != "validating")
    return Fail(Error(409, "batch_state_conflict", "Batch is not validating"));
  if (!input_) {
    auto initialized = Initialize(data);
    if (!initialized.ok()) return Fail(initialized);
  }
  const auto remaining = execution_deadline_at_ - std::time(nullptr);
  if (remaining <= 0)
    return Fail(Error(408, "batch_expired",
                       "Original Batch execution window expired"));
  access_.deadline = std::min(access_.deadline,
                               Clock::now() + std::chrono::seconds(remaining));
  const auto before = input_->Rows();
  while (input_->Rows() == before) {
    if (chunk_at_ == chunk_.size()) {
      if (next_chunk_ == source_.chunks.size()) return Finish();
      auto filled = Fill();
      if (!filled.ok()) return Fail(filled);
    }
    const auto newline = chunk_.find('\n', chunk_at_);
    const auto end = newline == std::string::npos ? chunk_.size() : newline + 1;
    const auto fragment = std::string_view(chunk_).substr(chunk_at_,
                                                         end - chunk_at_);
    auto fed = input_->Feed(fragment);
    if (!fed.ok()) return Fail(fed);
    chunk_at_ = end;
  }
  if (chunk_at_ == chunk_.size() && next_chunk_ == source_.chunks.size())
    return Finish();
  return {};
}
}  // namespace q4t::server
