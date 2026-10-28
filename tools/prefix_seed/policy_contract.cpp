#include "q4t/server/chat_contract.h"
#include "q4t/server/prefix_cache_policy.h"
#include "q4t/test.h"

#include <array>
#include <string>

namespace {
bool AcceptSeed(const std::string& value, bool scope = true) {
  const std::string prefix = "{\"prompt\":\"x\",\"prefix_cache_write_tokens\":";
  const std::string group = scope
      ? ",\"prefix_cache_scope\":\"" + std::string(64, 'a') + "\"}" : "}";
  q4t::io::Json req;
  return q4t::io::ParseJson(prefix + value + group, &req).ok() &&
         q4t::server::ValidateChatContract(req, "qwen3.8-flash-next", false).ok();
}
}  // namespace

Q4T_TEST(prefix_seed_transport_contract) {
  for (const auto* value : {"0", "1024", "8193", "131072"})
    Q4T_CHECK(AcceptSeed(value));
  Q4T_CHECK(AcceptSeed("0", false));
  Q4T_CHECK(!AcceptSeed("8192", false));
  for (const auto* value : {"null", "true", "[]", "\"8192\"", "-1",
                            "1023", "8192.5", "131073", "1e308"})
    Q4T_CHECK(!AcceptSeed(value));
  return true;
}

Q4T_TEST(prefix_seed_tokenized_boundary_contract) {
  using q4t::server::ValidatePrefixSeedBoundary;
  const std::array allowed{1024, 8193, 32768, 65536};
  Q4T_CHECK(ValidatePrefixSeedBoundary(0, {}, 1));
  Q4T_CHECK(ValidatePrefixSeedBoundary(8193, allowed, 8194));
  Q4T_CHECK(!ValidatePrefixSeedBoundary(8193, allowed, 8193));
  Q4T_CHECK(!ValidatePrefixSeedBoundary(8193, allowed, 8192));
  Q4T_CHECK(!ValidatePrefixSeedBoundary(8192, allowed, 9000));
  Q4T_CHECK(!ValidatePrefixSeedBoundary(1024, {}, 9000));
  return true;
}
