#pragma once

#include "q4t/server/worker_io_budget.h"

namespace q4t::server::detail {
// One owner, one reader. No remote pointer dereferencing or file IO occurs in
// this broker. Only immutable scalar syscall arguments are used for admission.
class WorkerIoBroker {
 public:
  enum class Result { kOk, kCapacity, kInvalid };
  WorkerIoBroker(int descriptor, WorkerIoBudget budget);
  ~WorkerIoBroker();
  WorkerIoBroker(const WorkerIoBroker&) = delete;
  WorkerIoBroker& operator=(const WorkerIoBroker&) = delete;
  int Descriptor() const { return descriptor_; }
  Result Pump();
  static bool Valid(const WorkerIoBudget& budget);

 private:
  int descriptor_;
  WorkerIoBudget remaining_;
};
}  // namespace q4t::server::detail
