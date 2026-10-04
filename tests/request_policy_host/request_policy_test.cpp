// Execute only after the optimization's first actual HTTP E2E test.
#include "q4t/model/moe_request_policy.h"
#include "q4t/test.h"

#include <limits>
#include <string>
#include <type_traits>

namespace {
using q4t::model::MoERequestPartition;
using q4t::model::ParseMoERequestPartitionMode;
}  // namespace

Q4T_TEST(request_policy_strict_opt_in) {
  Q4T_CHECK(ParseMoERequestPartitionMode(nullptr) == 0);
  Q4T_CHECK(ParseMoERequestPartitionMode("0") == 0);
  Q4T_CHECK(ParseMoERequestPartitionMode("1") == 1);
  for (const char* value : {"", "01", "2", "-1", "true", " 1", "1 ", "1\n"}) {
    Q4T_CHECK(ParseMoERequestPartitionMode(value) == -1);
  }
  return true;
}

Q4T_TEST(request_policy_default_preserves_global_mode) {
  const MoERequestPartition omitted;
  Q4T_CHECK(!omitted.Enabled());
  Q4T_CHECK(!omitted.HasRequest());
  Q4T_CHECK(omitted.ValidForward(1));
  Q4T_CHECK(omitted.PartitionMode(0) == 0);
  Q4T_CHECK(omitted.PartitionMode(1) == 1);
  Q4T_CHECK(omitted.PartitionMode(-1) == -1);
  Q4T_CHECK(omitted.WithBase(8192).Base() == 0);
  return true;
}

Q4T_TEST(request_policy_complete_input_boundary) {
  for (int length : {1, 1024, 4096, 8191, 8192}) {
    const MoERequestPartition request(length, "short-request");
    Q4T_CHECK(request.ValidForward(length));
    Q4T_CHECK(request.PartitionMode(1) == 0);
  }
  for (int length : {8193, 16385, 45056, 204800, 261887}) {
    const MoERequestPartition request(length, "long-request");
    Q4T_CHECK(request.ValidForward(8192));
    Q4T_CHECK(request.PartitionMode(1) == 1);
  }
  return true;
}

Q4T_TEST(request_policy_observed_baseline_preserves_global) {
  for (int length : {8192, 8193, 16385}) {
    const MoERequestPartition request(length, "observed-baseline", false);
    Q4T_CHECK(request.HasRequest());
    Q4T_CHECK(!request.Enabled());
    Q4T_CHECK(request.ValidForward(1));
    Q4T_CHECK(request.PartitionMode(0) == 0);
    Q4T_CHECK(request.PartitionMode(1) == 1);
    const auto tail = request.WithBase(length - 1);
    Q4T_CHECK(tail.ValidForward(1));
    Q4T_CHECK(!tail.Enabled());
    Q4T_CHECK(tail.PartitionMode(0) == 0);
    Q4T_CHECK(tail.RequestId() == request.RequestId());
  }
  return true;
}

Q4T_TEST(request_policy_chunk_and_singleton_keep_identity) {
  static_assert(std::is_copy_constructible_v<MoERequestPartition>);
  static_assert(!std::is_copy_assignable_v<MoERequestPartition>);
  const MoERequestPartition original(16385, "immutable-request");
  for (int base : {0, 8192, 16384}) {
    const auto chunk = original.WithBase(base);
    Q4T_CHECK(chunk.Enabled());
    Q4T_CHECK(chunk.RequestTokens() == 16385);
    Q4T_CHECK(chunk.RequestId() == "immutable-request");
    Q4T_CHECK(chunk.Base() == base);
    Q4T_CHECK(chunk.PartitionMode(1) == 1);
    Q4T_CHECK(chunk.ValidForward(base == 16384 ? 1 : 8192));
    Q4T_CHECK(original.Base() == 0);
  }
  const auto tail = original.WithBase(16384);
  Q4T_CHECK(!tail.ValidForward(2));
  Q4T_CHECK(!tail.ValidForward(8192));
  return true;
}

Q4T_TEST(request_policy_rejects_invalid_ranges_without_laundering) {
  for (int length : {-1, 0}) {
    const MoERequestPartition invalid(length, "invalid-request");
    Q4T_CHECK(!invalid.ValidForward(1));
    Q4T_CHECK(!invalid.WithBase(0).ValidForward(1));
    Q4T_CHECK(!invalid.WithBase(8192).ValidForward(1));
  }
  const MoERequestPartition request(8193, "range-request");
  for (int base : {-1, 8193, std::numeric_limits<int>::max()}) {
    Q4T_CHECK(!request.WithBase(base).ValidForward(1));
  }
  for (int count : {-1, 0, 8194, std::numeric_limits<int>::max()}) {
    Q4T_CHECK(!request.ValidForward(count));
  }
  const MoERequestPartition largest(std::numeric_limits<int>::max(), "large");
  const auto tail = largest.WithBase(std::numeric_limits<int>::max() - 1);
  Q4T_CHECK(tail.ValidForward(1));
  Q4T_CHECK(!tail.ValidForward(2));
  return true;
}

Q4T_TEST(request_policy_log_identity_is_bounded_and_unambiguous) {
  for (const std::string& id : {std::string(), std::string(129, 'a'),
                                std::string("two words"), std::string("x=y"),
                                std::string("x\ny"), std::string("x\0y", 3),
                                std::string("x/y"), std::string("中文")}) {
    Q4T_CHECK(!MoERequestPartition(8193, id).ValidForward(1));
    Q4T_CHECK(!MoERequestPartition(8193, id, false).ValidForward(1));
  }
  Q4T_CHECK(MoERequestPartition(8193, "Az09-_request").ValidForward(1));
  Q4T_CHECK(MoERequestPartition(8193, std::string(128, 'a')).ValidForward(1));
  return true;
}

Q4T_TEST(request_policy_opposite_requests_do_not_inherit_selection) {
  const MoERequestPartition long_request(16385, "long");
  const MoERequestPartition short_request(8192, "short");
  const auto long_tail = long_request.WithBase(16384);
  Q4T_CHECK(short_request.PartitionMode(1) == 0);
  Q4T_CHECK(short_request.Base() == 0);
  Q4T_CHECK(short_request.RequestId() == "short");
  Q4T_CHECK(long_tail.PartitionMode(1) == 1);
  Q4T_CHECK(long_request.Base() == 0);
  Q4T_CHECK(long_tail.ValidForward(1));
  return true;
}
