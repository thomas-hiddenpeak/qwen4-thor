// Private, bounded inline Batch payloads. Large inputs/results retain their
// original immutable snapshot/chunk representation.
#pragma once

#include "q4t/server/object_store.h"

namespace q4t::server {
inline constexpr size_t kBatchInlineBytes = 8192;
bool IsBatchPage(const io::Json& data);
bool ValidBatchInlineInput(const io::Json& value);
bool ValidBatchInlineResult(const io::Json& row);
// Both ordinary writes and transactions enforce the frozen prefix. A mutable
// scheduling page must never become a way to rewrite an accepted input/result.
ObjectReply CheckBatchPageMutation(const io::Json& data,
                                   const io::Json* previous);
}  // namespace q4t::server
