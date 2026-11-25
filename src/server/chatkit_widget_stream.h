#pragma once

#include "q4t/io/json.h"

namespace q4t::server::chatkit_detail {
bool ValidWidget(const io::Json& root, bool require_unique_ids);
// Each update applies to the widget produced by the preceding snapshot.
io::Json WidgetUpdates(const io::Json& before, const io::Json& after);
}  // namespace q4t::server::chatkit_detail
