#pragma once

#include "q4t/server/input_snapshot.h"

namespace q4t::server {
// Bounded structural identity: object ordering is irrelevant, exact numbers
// retain mathematical identity, and large inline file strings are hashed
// without making another escaped JSON copy. Never an authentication key.
Status InputValueIdentity(const io::Json& value, std::string* sha256,
                          size_t max_values = 262144);
// Private paths identify typed content parts within an immutable public item.
// They are never supplied through the public request schema.
ObjectReply BindInputSnapshot(const io::Json& item, std::string_view path,
                              const InputSnapshotRef& reference,
                              io::Json* binding);
ObjectReply MergeInputSnapshots(const io::Json& added, const io::Json& items,
                                io::Json* bindings);
// Subset for published items or a conversation after deletion. Any retained
// binding is checked against its exact original part, not just its item ID.
ObjectReply SelectInputSnapshots(const io::Json& bindings,
                                 const io::Json& items, io::Json* selected);
}  // namespace q4t::server
