#pragma once

namespace q4t::service {

// Host-only dispatch to source-bound management tools; never initializes CUDA.
int RunService(int argc, char** argv);

}  // namespace q4t::service
