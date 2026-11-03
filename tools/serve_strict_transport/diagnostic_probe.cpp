// Compile with and without the validation macro; never starts a server.
#include <vector>

#include "q4t/server/serve_fault.h"

int main() {
  std::vector<int32_t> tokens(5000, 7);
  tokens.back() = 999;
  q4t::server::detail::ServeGeneratedTokens("unselected", tokens);
  q4t::server::detail::ServeGeneratedTokens("selected", tokens);
  q4t::server::detail::ServeGeneratedTokens("selected", {});
}
