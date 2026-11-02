#pragma once

#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/output_constraints.h"
#include "q4t/status.h"

namespace q4t::server {

// Transport-decoded media, in template order. No model or CUDA ownership.
struct VisionItem {
  enum Kind { kImage, kVideo };
  Kind kind = kImage;
  std::vector<std::string> frames;
};

// Shared by generation and preflight after ValidateChatContract. The same
// constraint adapter and renderer determine the actual generation prefix.
Status PrepareChatPrompt(const io::Json& request,
                         const OutputConstraints& constraints,
                         std::string* prompt, std::vector<VisionItem>* items,
                         std::string* error_param);

}  // namespace q4t::server
