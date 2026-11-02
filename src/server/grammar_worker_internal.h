#pragma once

#include <cstdint>

namespace q4t::server::detail {
// Private, native-endian protocol between two execs of the same pinned ELF.
// No serialized library objects cross the boundary. Every frame is bounded.
inline constexpr uint32_t kGrammarWireMagic = 0x51475431;
inline constexpr uint32_t kGrammarInputLimit = 32 * 1024 * 1024;
enum class WorkerCode : uint32_t { kOk, kResource, kInvalid, kInternal };
struct GrammarReply {
  uint32_t magic = kGrammarWireMagic;
  WorkerCode code = WorkerCode::kOk;
  uint32_t flags = 0;  // bit 0: terminated; bit 1: mask is restrictive
  uint32_t words = 0;
};
static_assert(sizeof(GrammarReply) == 16);
}  // namespace q4t::server::detail
