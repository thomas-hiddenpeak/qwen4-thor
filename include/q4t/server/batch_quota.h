// Durable Batch queue accounting. Only the exclusive storage worker mutates
// this index; it is rebuilt from records after journal recovery, never from
// monitoring or an independently written counter file.
#pragma once

#include <map>
#include <span>
#include <string>

#include "q4t/server/object_store.h"

namespace q4t::server {
struct BatchQueueCharge {
  std::string model;
  uint64_t tokens = 0;
  bool unknown = false;
};
BatchQueueCharge InspectBatchQueueCharge(std::string_view kind,
                                         const io::Json& data);
// Preserve metered prefixes and their canonical model even after termination.
ObjectReply CheckBatchBudgetMutation(const io::Json& data,
                                     const io::Json* previous);
struct BatchInputRecount {
  uint64_t rows = 0, tokens = 0, total_rows = 0;
};
bool ParseBatchInputRecount(const io::Json& value, BatchInputRecount* output);
bool ValidBatchInputRecount(const io::Json& data);

struct BatchQueueChange {
  BatchQueueCharge before, after;
  // Only a verified addition of budget to an already frozen legacy row.
  // Recounting old work is not admitting new input under a reduced limit.
  bool recount = false;
};
class BatchQueueQuota {
 public:
  void Add(const std::string& owner, const BatchQueueCharge& charge);
  void Remove(const std::string& owner, const BatchQueueCharge& charge);
  // Does not mutate accounting. Called before the journal commit decision.
  // Over-limit restored state can drain; only increases are denied.
  ObjectReply Check(const std::string& owner,
                    std::span<const BatchQueueChange> changes,
                    uint64_t model_limit, uint64_t client_model_limit) const;

 private:
  // At most 100000 records, each up to 2^53-1 tokens. Exact subtraction must
  // remain possible even when restoring a directory above a reduced limit.
  using Wide = unsigned __int128;
  struct Usage {
    Wide tokens = 0;
    uint64_t unknown = 0;
    bool Empty() const { return !tokens && !unknown; }
  };
  struct Model {
    Usage total;
    std::map<std::string, Usage> clients;
  };
  std::map<std::string, Model> models_;
};
}  // namespace q4t::server
