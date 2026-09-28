#include <fstream>
#include <iostream>

#include "q4t/trace/router_trace.h"

int main(int argc, char** argv) {
  if (argc != 2) {
    std::cerr << "usage: q4t_router_trace_check TRACE.bin\n";
    return 2;
  }
  std::ifstream input(argv[1], std::ios::binary);
  q4t::trace::TraceSummary summary;
  const auto status = q4t::trace::CheckRouterTrace(input, &summary);
  if (!status.ok()) {
    std::cerr << status.message() << '\n';
    return 1;
  }
  std::cout << "{\"structurally_complete\":true,\"requests\":"
            << summary.requests
            << ",\"successful_requests\":" << summary.successful_requests
            << ",\"cancelled_requests\":" << summary.cancelled_requests
            << ",\"failed_requests\":" << summary.failed_requests
            << ",\"committed_prefill_rows\":" << summary.committed_prefill_rows
            << ",\"committed_decode_rows\":" << summary.committed_decode_rows
            << ",\"route_ids\":" << summary.route_ids
            << ",\"output_tokens\":" << summary.output_tokens << "}\n";
  // Diagnostic records remain visible, including runs with some successes.
  return summary.successful_requests == 0 || summary.cancelled_requests != 0 ||
                 summary.failed_requests != 0
             ? 3
             : 0;
}
