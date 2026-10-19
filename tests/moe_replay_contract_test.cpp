#include "q4t/model/moe_replay.h"
#include "q4t/test.h"

#include <limits>

Q4T_TEST(moe_replay_identity_and_ownership) {
  using namespace q4t::model;
  MoEReplayIdentity source;
  source.model[0] = 1;
  source.weights[0] = 2;
  source.hidden[0] = 3;
  source.binary[0] = 4;
  source.layer = 2;
  source.position = 1;
  source.rows = 1;
  source.experts = 512;
  source.top_k = 2;
  source.tokens = {42, 43};
  MoEReplayRecord record;
  std::vector<int32_t> ids = {511, 0};
  Q4T_CHECK(record.Prepare(source, source, ids).ok());
  ids[0] = 7;
  Q4T_CHECK(record.ids()[0] == 511 && record.ids()[1] == 0);
  ids[0] = 511;
  for (int field = 0; field < 12; ++field) {
    auto target = source;
    switch (field) {
      case 0: target.model[0]++; break;
      case 1: target.weights[0]++; break;
      case 2: target.hidden[0]++; break;
      case 3: target.binary[0]++; break;
      case 4: target.role = "draft"; break;
      case 5: target.layer++; break;
      case 6: target.position++; break;
      case 7: target.rows++; break;
      case 8: target.experts--; break;
      case 9: target.top_k--; break;
      case 10: target.tokens[0]++; break;
      case 11: target.model = {}; break;
    }
    Q4T_CHECK(!record.Prepare(source, target, ids).ok());
    Q4T_CHECK(!record.ready());
  }
  for (const std::vector<int32_t>& bad :
       {std::vector<int32_t>{}, {0}, {0, 1, 2}, {0, 0}, {-1, 0}, {512, 0}}) {
    Q4T_CHECK(!record.Prepare(source, source, bad).ok());
  }
  auto target = source;
  target.binary[0]++;
  MoEReplayCompatibility compatibility{source.binary, target.binary,
                                       "explicit test build difference"};
  Q4T_CHECK(record.Prepare(source, target, ids, &compatibility).ok());
  compatibility.allowed_difference.clear();
  Q4T_CHECK(!record.Prepare(source, target, ids, &compatibility).ok());
  target = source;
  target.position = std::numeric_limits<uint64_t>::max();
  Q4T_CHECK(!record.Prepare(target, target, ids).ok());
  target = source;
  target.tokens.pop_back();
  Q4T_CHECK(!record.Prepare(target, target, ids).ok());
  target = source;
  target.tokens.push_back(44);
  Q4T_CHECK(!record.Prepare(target, target, ids).ok());
  Q4T_CHECK(record.Prepare(source, source, ids).ok());
  Q4T_CHECK(record.Prepare(record.source(), record.execution(),
                           record.ids()).ok());
  source.tokens[0]++;
  Q4T_CHECK(record.source().tokens[0] == 42);
  return true;
}
