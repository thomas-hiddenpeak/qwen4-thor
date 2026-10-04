// Execute only after the optimization's first actual HTTP E2E test.
#include "q4t/model/moe_partition_log.h"
#include "q4t/model/moe_request_policy.h"
#include "q4t/test.h"

#include <limits>

namespace {
using q4t::model::MoEPartitionLogPhase;
using q4t::model::MoERequestPartition;
using q4t::model::ParseMoEDecodePartitionLogQuietMode;
using q4t::model::ShouldEmitMoEPartitionLog;
using q4t::model::SingleDecodeLogPhase;
}  // namespace

Q4T_TEST(decode_partition_log_strict_opt_in) {
  Q4T_CHECK(ParseMoEDecodePartitionLogQuietMode(nullptr) == 0);
  Q4T_CHECK(ParseMoEDecodePartitionLogQuietMode("0") == 0);
  Q4T_CHECK(ParseMoEDecodePartitionLogQuietMode("1") == 1);
  for (const char* value : {"", "01", "2", "-1", "true", " 1", "1 ", "1\n"}) {
    Q4T_CHECK(ParseMoEDecodePartitionLogQuietMode(value) == -1);
  }
  return true;
}

Q4T_TEST(decode_partition_log_default_preserves_records) {
  const int default_mode = ParseMoEDecodePartitionLogQuietMode(nullptr);
  for (const auto phase : {MoEPartitionLogPhase::kUnknown,
                           MoEPartitionLogPhase::kSingleDecode}) {
    for (int tokens : {1, 2, 8192}) {
      for (bool has_request : {false, true}) {
        Q4T_CHECK(ShouldEmitMoEPartitionLog(1, default_mode, phase, tokens,
                                           has_request));
        Q4T_CHECK(ShouldEmitMoEPartitionLog(1, 0, phase, tokens, has_request));
      }
    }
  }
  return true;
}

Q4T_TEST(decode_partition_log_requires_explicit_phase) {
  const MoERequestPartition omitted;
  Q4T_CHECK(omitted.ValidForward(1));
  Q4T_CHECK(!ShouldEmitMoEPartitionLog(
      1, 1, SingleDecodeLogPhase(1), 1, omitted.HasRequest()));
  // A direct singleton prefill caller can omit request identity as well.
  Q4T_CHECK(ShouldEmitMoEPartitionLog(
      1, 1, MoEPartitionLogPhase::kUnknown, 1, omitted.HasRequest()));
  return true;
}

Q4T_TEST(decode_partition_log_keeps_prefill_singletons) {
  for (int length : {1, 8193, 16385}) {
    for (bool enabled : {false, true}) {
      const MoERequestPartition request(length, "prefill", enabled);
      const auto tail = request.WithBase(length - 1);
      Q4T_CHECK(tail.ValidForward(1));
      Q4T_CHECK(tail.HasRequest());
      Q4T_CHECK(ShouldEmitMoEPartitionLog(
          1, 1, MoEPartitionLogPhase::kUnknown, 1, tail.HasRequest()));
      Q4T_CHECK(tail.PartitionMode(1) ==
                (enabled && length <= 8192 ? 0 : 1));
    }
  }
  return true;
}

Q4T_TEST(decode_partition_log_excludes_other_batch_shapes) {
  Q4T_CHECK(SingleDecodeLogPhase(1) ==
            MoEPartitionLogPhase::kSingleDecode);
  for (int tokens : {-1, 0, 2, 8, std::numeric_limits<int>::max()}) {
    Q4T_CHECK(SingleDecodeLogPhase(tokens) == MoEPartitionLogPhase::kUnknown);
    Q4T_CHECK(ShouldEmitMoEPartitionLog(
        1, 1, SingleDecodeLogPhase(tokens), tokens, false));
    // An inconsistent phase must not turn other shapes into single decode.
    Q4T_CHECK(ShouldEmitMoEPartitionLog(
        1, 1, MoEPartitionLogPhase::kSingleDecode, tokens, false));
  }
  return true;
}

Q4T_TEST(decode_partition_log_never_enables_global_off_records) {
  for (int quiet : {0, 1}) {
    for (const auto phase : {MoEPartitionLogPhase::kUnknown,
                             MoEPartitionLogPhase::kSingleDecode}) {
      for (bool has_request : {false, true}) {
        Q4T_CHECK(!ShouldEmitMoEPartitionLog(0, quiet, phase, 1, has_request));
      }
    }
  }
  return true;
}

Q4T_TEST(decode_partition_log_prefill_identity_prevents_suppression) {
  // Even a contradictory caller phase cannot suppress a prefill identity.
  for (bool enabled : {false, true}) {
    const auto tail = MoERequestPartition(8193, "tail", enabled).WithBase(8192);
    Q4T_CHECK(tail.ValidForward(1));
    Q4T_CHECK(ShouldEmitMoEPartitionLog(
        1, 1, MoEPartitionLogPhase::kSingleDecode, 1, tail.HasRequest()));
  }
  return true;
}
