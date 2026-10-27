#pragma once

#include "q4t/model/model.h"
#include "q4t/mtp/mtp.h"
#include "q4t/server/session_cache.h"

namespace q4t::server {

// Effective state at a completed target+shifted-draft prefix. Page tables and
// all workspace/checkpoint ownership stay with the destination slot. Text
// rope_delta must be zero; every effective RoPE position is copied explicitly.
Status SessionStateRanges(const model::Model& model, const mtp::MtpModel& draft,
                           int slot, int prefix, uint16_t* rolling_g,
                           uint16_t* last_main_trunk,
                           std::vector<SessionRange>* ranges);

}  // namespace q4t::server
