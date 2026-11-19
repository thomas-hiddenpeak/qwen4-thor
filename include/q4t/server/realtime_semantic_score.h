#pragma once

#include "q4t/io/json.h"

namespace q4t::server {
// Local model estimate, not calibrated probability or a remote model claim.
Status BuildRealtimeSemanticRequest(std::string_view model,
                                    std::string_view actual_transcript,
                                    io::Json* output);
Status ParseRealtimeSemanticScore(std::string_view content,
                                  std::string_view finish_reason,
                                  double* probability);
}  // namespace q4t::server
