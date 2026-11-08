#pragma once

#include <string>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// A canonical model request plus its external response dialect. Synthetic
// call IDs exist only inside the model/template pipeline, never on the legacy
// wire or as a client authorization identity.
struct LegacyToolRequest {
  io::Json request;
  bool legacy_output = false;
};

bool HasLegacyTools(const io::Json& request);
Status NormalizeLegacyTools(const io::Json& request, LegacyToolRequest* output,
                            std::string* error_param = nullptr);
void LegacyToolErrorParam(const io::Json& original, std::string* error_param);

}  // namespace q4t::server
