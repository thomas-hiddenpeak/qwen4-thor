#pragma once

#include <cstddef>
#include <span>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Validate identified assistant/tool batches. When requested, return message
// indices in rendering order: only consecutive tool results can move. No JSON
// or content copies. Reordered batches must contain only text results, so
// vision features still match their original content order. Batches without IDs
// retain legacy template behavior. render_order is cleared on failure; nullptr
// performs validation only.
Status PlanToolResultOrder(const io::Json& messages,
                           std::vector<size_t>* render_order = nullptr,
                           std::span<const std::string> async_call_ids = {});

}  // namespace q4t::server
