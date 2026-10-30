#include "q4t/runtime/memory_budget.h"
#include "q4t/server/server_options.h"

#include <iostream>
#include <string_view>

int main() {
  q4t::server::ServerOptions options;
  options.model_dir = "read-only-fixture-not-opened";
  const std::string_view args[] = {
      "--no-mtp", "--max-prefill", "1", "--max-seq", "2", "--max-len", "8192"};
  const auto status = q4t::server::ParseServerOptions(args, &options);
  q4t::runtime::BudgetRequest request;
  request.max_len = options.max_len;
  request.max_seq = options.max_seq;
  request.max_prefill = options.max_prefill;
  const auto budget = q4t::runtime::ComputeMemoryBudget(
      {}, request, 84000000000ULL, q4t::runtime::ReadMemTotal());
  std::cout << "options_accepted=" << status.ok()
            << " budget_feasible=" << budget.feasible
            << " effective_max_seq=" << budget.max_seq
            << " forward_rows=" << budget.max_prefill
            << " full_decode_batch_fits="
            << (budget.max_seq <= budget.max_prefill) << '\n';
}
