#include "q4t/server/server_options.h"
#include "q4t/test.h"

#include <initializer_list>
#include <limits>
#include <string_view>

namespace {
using q4t::server::CapabilitiesFor;
using q4t::server::ServerOptions;
ServerOptions Defaults() {
  ServerOptions options;
  options.model_dir = "/read-only-model";
  return options;
}
bool Parse(std::initializer_list<std::string_view> args, ServerOptions* out) {
  return q4t::server::ParseServerOptions({args.begin(), args.size()}, out).ok();
}
}  // namespace

Q4T_TEST(serve_default_is_single_text_greedy) {
  auto options = Defaults();
  Q4T_CHECK(Parse({}, &options));
  const auto capabilities = CapabilitiesFor(options);
  Q4T_CHECK(options.max_seq == 1 && options.no_mtp && !options.allow_media);
  Q4T_CHECK(!capabilities.Experimental());
  Q4T_CHECK(Parse({"--max-seq", "1", "--max-prefill", "8192", "--max-len",
                   "208896", "--no-mtp"},
                  &options));
  Q4T_CHECK(!CapabilitiesFor(options).Experimental());
  return true;
}

Q4T_TEST(serve_capabilities_allow_explicit_combinations) {
  for (int mask = 0; mask < 8; ++mask) {
    auto options = Defaults();
    if (mask & 1) Q4T_CHECK(Parse({"--mtp"}, &options));
    if (mask & 2) Q4T_CHECK(Parse({"--allow-media"}, &options));
    if (mask & 4) Q4T_CHECK(Parse({"--max-seq", "2"}, &options));
    const auto c = CapabilitiesFor(options);
    Q4T_CHECK(c.mtp == bool(mask & 1));
    Q4T_CHECK(c.media == bool(mask & 2));
    Q4T_CHECK(c.multiple_sequences == bool(mask & 4));
    Q4T_CHECK(c.Experimental() == (mask != 0));
  }
  auto options = Defaults();
  Q4T_CHECK(Parse({"--mtp", "--no-mtp"}, &options));
  Q4T_CHECK(!CapabilitiesFor(options).mtp);
  Q4T_CHECK(Parse({"--no-mtp", "--mtp"}, &options));
  Q4T_CHECK(CapabilitiesFor(options).mtp);
  return true;
}

Q4T_TEST(serve_rejects_malformed_numeric_options) {
  for (const auto flag : {"--port", "--max-seq", "--max-len", "--max-prefill",
                          "--max-tokens", "--mem-fraction"}) {
    for (const auto value : {"", "junk", "1tail", " 1", "1 ", "--mtp",
                             "9999999999999999999999999999999999999999"}) {
      auto options = Defaults();
      Q4T_CHECK(!Parse({flag, value}, &options));
      Q4T_CHECK(options.max_seq == 1 && options.no_mtp);
    }
    auto options = Defaults();
    Q4T_CHECK(!Parse({flag}, &options));
  }
  for (const auto value : {"0", "-1", "1.5", "2147483647"}) {
    auto options = Defaults();
    Q4T_CHECK(!Parse({"--max-seq", value}, &options));
  }
  for (const auto value : {"nan", "inf", "-inf", "0", "-0.1", "1.01"}) {
    auto options = Defaults();
    Q4T_CHECK(!Parse({"--mem-fraction", value}, &options));
  }
  return true;
}

Q4T_TEST(serve_validates_before_load_and_preserves_failed_parse) {
  auto options = Defaults();
  Q4T_CHECK(!Parse({"--mtp", "--port", "65536"}, &options));
  Q4T_CHECK(options.no_mtp && options.port == 8000);
  Q4T_CHECK(!Parse({"--host", "not-an-address"}, &options));
  Q4T_CHECK(!Parse({"--model-dir", ""}, &options));
  Q4T_CHECK(!Parse({"--unknown"}, &options));
  Q4T_CHECK(!Parse({"--max-tokens", "0"}, &options));
  Q4T_CHECK(!Parse({"--max-len", "-1"}, &options));
  Q4T_CHECK(!Parse({"--max-prefill", "-1"}, &options));
  Q4T_CHECK(Parse({"--port", "65535", "--max-prefill", "0", "--max-len", "0",
                   "--mem-fraction", "1", "--no-budget"},
                  &options));
  options.max_seq = 0;
  Q4T_CHECK(!q4t::server::ValidateServerOptions(options).ok());
  options = Defaults();
  options.mem_fraction = std::numeric_limits<double>::quiet_NaN();
  Q4T_CHECK(!q4t::server::ValidateServerOptions(options).ok());
  return true;
}
