#pragma once

#include "q4t/io/json.h"
#include "q4t/server/object_store.h"

namespace q4t::server::chatkit_detail {
// A workflow can present one of its registered functions as a browser form.
// Keep the tool call as the management object and bind answers to its call ID.
io::Json PendingStructuredInput(const io::Json& arguments);
ObjectReply SubmitStructuredInput(const io::Json& submission, io::Json* form);
}  // namespace q4t::server::chatkit_detail
