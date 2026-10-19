#pragma once

#include "q4t/model/moe_replay.h"

namespace q4t::model {

// Versioned little-endian file plus SHA256 checksum. Source identity only;
// execution identity is freshly bound by the reader/loader, never trusted from
// the file. Bounded to 16 MiB and consumed exactly, including the checksum.
Status WriteMoEReplay(const std::string& path, const MoEReplayRecord& record);
Status ReadMoEReplay(const std::string& path, MoEReplayRecord* record);

}  // namespace q4t::model
