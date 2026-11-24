#pragma once

#include "q4t/server/object_store.h"

namespace q4t::server::eval_detail {
// Shared by model input templates and graders. Only source text is expanded.
// Both namespaces come from the same already-authorized dataset row.
const io::Json* ResolveVariable(std::string_view path, const io::Json& row);
ObjectReply RenderText(std::string_view text, const io::Json& row,
                       std::string_view param, size_t* remaining,
                       std::string* rendered);
ObjectReply GradeString(const io::Json& criterion, const io::Json& row,
                        size_t* remaining);
}  // namespace q4t::server::eval_detail
