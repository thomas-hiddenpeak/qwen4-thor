#include "q4t/model/sequence.h"
#include "q4t/test.h"

namespace {
using q4t::Status;
using q4t::model::CompleteSequenceWork;
using q4t::model::ModelSequence;
using Stage = ModelSequence::Stage;
const int32_t kPrompt[] = {17, 23, 31};

Status Finish(ModelSequence& seq, const Status& result = Status()) {
  ModelSequence* pointer = &seq;
  return CompleteSequenceWork({&pointer, 1}, result, [] { return Status(); });
}
}  // namespace

Q4T_TEST(sequence_commit_requires_completion) {
  ModelSequence seq;
  Q4T_CHECK(seq.Begin(1).ok());
  Q4T_CHECK(seq.Submit(kPrompt, Stage::kDecode, 16).ok());
  Q4T_CHECK(seq.position == 0 && seq.history.empty());
  Q4T_CHECK(seq.SubmittedPosition() == 3 && seq.HasPending());
  Q4T_CHECK(seq.stage == Stage::kPrefill);
  bool observed = false;
  ModelSequence* pointer = &seq;
  Q4T_CHECK(CompleteSequenceWork({&pointer, 1}, Status(), [&] {
              observed =
                  seq.HasPending() && seq.position == 0 && seq.history.empty();
              return Status();
            }).ok());
  Q4T_CHECK(observed && seq.position == 3 && seq.seq_id == 1);
  Q4T_CHECK(seq.history == std::vector<int32_t>({17, 23, 31}));
  Q4T_CHECK(seq.stage == Stage::kDecode && !seq.HasPending());
  return true;
}

Q4T_TEST(sequence_rejects_reentry_and_early_recycle) {
  ModelSequence seq;
  Q4T_CHECK(seq.Begin(0).ok());
  Q4T_CHECK(seq.Submit(kPrompt, Stage::kDecode, 16).ok());
  Q4T_CHECK(!seq.Submit(kPrompt, Stage::kDecode, 16).ok());
  Q4T_CHECK(!seq.Begin(1).ok());
  Q4T_CHECK(!seq.End().ok());
  Q4T_CHECK(seq.stage == Stage::kFailed && seq.HasPending());
  Q4T_CHECK(!Finish(seq).ok());
  Q4T_CHECK(!seq.HasPending() && seq.position == 0);
  Q4T_CHECK(seq.End().ok() && seq.Begin(1).ok());
  return true;
}

Q4T_TEST(sequence_failed_submission_still_drains) {
  ModelSequence seq;
  Q4T_CHECK(seq.Begin(0).ok());
  Q4T_CHECK(seq.Submit(kPrompt, Stage::kDecode, 16).ok());
  bool waited = false;
  ModelSequence* pointer = &seq;
  Q4T_CHECK(!CompleteSequenceWork({&pointer, 1}, Status::Fail("launch"), [&] {
               waited = seq.HasPending();
               return Status();
             }).ok());
  Q4T_CHECK(waited && seq.stage == Stage::kFailed && !seq.HasPending());
  Q4T_CHECK(seq.position == 0 && seq.history.empty());
  Q4T_CHECK(!seq.Submit(kPrompt, Stage::kDecode, 16).ok());
  return true;
}

Q4T_TEST(sequence_completion_failure_preserves_prefix) {
  for (const char* failure : {"copy", "sync"}) {
    ModelSequence seq;
    Q4T_CHECK(seq.Begin(0).ok());
    Q4T_CHECK(seq.Submit(kPrompt, Stage::kDecode, 16).ok());
    Q4T_CHECK(Finish(seq).ok());
    Q4T_CHECK(seq.Submit(kPrompt, Stage::kDecode, 16).ok());
    ModelSequence* pointer = &seq;
    Q4T_CHECK(!CompleteSequenceWork({&pointer, 1}, Status(), [&] {
                 return Status::Fail(failure);
               }).ok());
    Q4T_CHECK(seq.position == 3 && seq.history.size() == 3);
    Q4T_CHECK(seq.stage == Stage::kFailed && !seq.HasPending());
  }
  return true;
}

Q4T_TEST(sequence_invalid_input_does_not_poison_prefix) {
  ModelSequence seq;
  Q4T_CHECK(!seq.Submit(kPrompt, Stage::kDecode, 16).ok());
  Q4T_CHECK(seq.Begin(0).ok());
  Q4T_CHECK(!seq.Submit(kPrompt, Stage::kDecode, 2).ok());
  Q4T_CHECK(!seq.Submit({}, Stage::kDecode, 16).ok());
  Q4T_CHECK(!seq.Submit(kPrompt, Stage::kIdle, 16).ok());
  Q4T_CHECK(seq.stage == Stage::kPrefill && !seq.HasPending());
  Q4T_CHECK(seq.Submit(kPrompt, Stage::kDecode, 3).ok());
  Q4T_CHECK(Finish(seq).ok());
  Q4T_CHECK(!seq.Submit(kPrompt, Stage::kDecode, 3).ok());
  Q4T_CHECK(seq.stage == Stage::kDecode && seq.position == 3);
  return true;
}

Q4T_TEST(sequence_chunk_and_decode_share_commit) {
  ModelSequence seq;
  Q4T_CHECK(seq.Begin(0).ok());
  Q4T_CHECK(seq.Submit(kPrompt, Stage::kPrefill, 16).ok());
  Q4T_CHECK(Finish(seq).ok());
  Q4T_CHECK(seq.stage == Stage::kPrefill && seq.position == 3);
  Q4T_CHECK(seq.Submit(kPrompt, Stage::kDecode, 16).ok());
  Q4T_CHECK(Finish(seq).ok());
  const int32_t token = 41;
  Q4T_CHECK(seq.Submit({&token, 1}, Stage::kDecode, 16).ok());
  Q4T_CHECK(seq.position == 6 && seq.SubmittedPosition() == 7);
  Q4T_CHECK(Finish(seq).ok());
  Q4T_CHECK(seq.position == 7 && seq.history.back() == 41);
  return true;
}

Q4T_TEST(sequence_batch_failure_is_atomic) {
  ModelSequence a, b;
  Q4T_CHECK(a.Begin(1).ok() && b.Begin(0).ok());
  Q4T_CHECK(a.Submit(kPrompt, Stage::kDecode, 16).ok());
  Q4T_CHECK(b.Submit(kPrompt, Stage::kDecode, 16).ok());
  b.Fail();  // Cancellation or failed launch before the completion boundary.
  ModelSequence* batch[] = {&a, &b};
  int waits = 0;
  Q4T_CHECK(!CompleteSequenceWork(batch, Status(), [&] {
               ++waits;
               return Status();
             }).ok());
  Q4T_CHECK(waits == 1 && a.position == 0 && b.position == 0);
  Q4T_CHECK(a.stage == Stage::kFailed && b.stage == Stage::kFailed);
  Q4T_CHECK(!a.HasPending() && !b.HasPending());
  return true;
}

Q4T_TEST(sequence_rejects_duplicate_completion) {
  ModelSequence seq;
  Q4T_CHECK(seq.Begin(0).ok());
  Q4T_CHECK(seq.Submit(kPrompt, Stage::kDecode, 16).ok());
  Q4T_CHECK(Finish(seq).ok());
  Q4T_CHECK(!Finish(seq).ok());
  Q4T_CHECK(seq.position == 3 && seq.history.size() == 3);
  Q4T_CHECK(seq.Begin(0).ok());
  Q4T_CHECK(seq.Submit(kPrompt, Stage::kDecode, 16).ok());
  ModelSequence* duplicate[] = {&seq, &seq};
  Q4T_CHECK(
      !CompleteSequenceWork(duplicate, Status(), [] { return Status(); }).ok());
  Q4T_CHECK(seq.position == 0 && seq.stage == Stage::kFailed);
  return true;
}
