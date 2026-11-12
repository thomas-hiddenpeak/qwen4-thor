#include "q4t/service/ingress_proxy.h"

#include <cstring>
#include <algorithm>
#include <iostream>
#include <iterator>
#include <stdexcept>

#include "q4t/service/guardian_wire.h"
#include "q4t/test.h"

namespace {
using namespace q4t::service::ingress;
namespace w = q4t::service::wire;
using q4t::io::Json;
using q4t::server::HttpRequestHeaders;

Json Policy(std::string_view mode = "forwarded", std::string_view prefix = "",
            bool strip = false) {
  const auto proxy =
      mode.empty()
          ? "null"
          : "{\"trusted_cidrs\":[\"127.0.0.1/32\",\"10.0.0.0/8\",\"::1/128\","
            "\"fd00::/8\"],\"header\":\"" +
                std::string(mode) +
                "\",\"allow_stripped_prefix\":" + (strip ? "true}" : "false}");
  Json value;
  if (!w::Parse("{\"keep_alive_requests\":3,\"keep_alive_idle_seconds\":1,"
                "\"request_timeout_seconds\":2,\"path_prefix\":\"" +
                    std::string(prefix) + "\",\"proxy\":" + proxy + "}",
                &value))
    throw std::runtime_error("fixture policy");
  return value;
}
void Replace(Json* value, const char* key, Json replacement) {
  for (auto& field : value->object)
    if (field.first == key) {
      field.second = std::move(replacement);
      return;
    }
  throw std::runtime_error("missing field");
}
bool Same(const Address& actual, const char* expected) {
  const auto address = Address::Parse(expected);
  return actual.family == address.family && actual.bytes == address.bytes;
}
}  // namespace

Q4T_TEST(ingress_http_policy_validation_is_transactional) {
  HttpPolicy policy;
  Q4T_CHECK(policy.Load(Policy("forwarded", "/llm")));
  Q4T_CHECK(policy.keep_alive_requests == 3 &&
            policy.keep_alive_idle_ms == 1000);
  for (const auto name : {"keep_alive_requests", "keep_alive_idle_seconds",
                          "request_timeout_seconds"}) {
    for (const auto& invalid :
         {w::Unsigned(0), w::Unsigned(1001), w::Boolean(true)}) {
      auto value = Policy();
      Replace(&value, name, invalid);
      Q4T_CHECK(!policy.Load(value) && policy.path_prefix == "/llm");
    }
  }
  auto extra = Policy();
  extra.object.emplace_back("extra", Json{});
  Q4T_CHECK(!policy.Load(extra));
  for (const auto bad : {"/", "/llm/", "/llm//v1", "/../llm", "/llm/.", "llm",
                         "/llm%2f", "/llm?x", "/llm#"}) {
    Q4T_CHECK(!policy.Load(Policy("forwarded", bad)));
  }
  Q4T_CHECK(!policy.Load(Policy("unknown")));
  Q4T_CHECK(!policy.Load(Policy("forwarded", "", true)));
  Q4T_CHECK(policy.Load(Policy("", "")) && policy.path_prefix.empty());
  return true;
}

Q4T_TEST(ingress_proxy_untrusted_metadata_is_never_authority) {
  HttpPolicy policy;
  Q4T_CHECK(policy.Load(Policy()));
  HttpRequestHeaders fields;
  fields.host = "local";
  fields.forwarded = "for=10.0.0.9;host=forged;proto=https";
  fields.forwarded_for = "10.0.0.8";
  fields.forwarded_host = "forged";
  fields.forwarded_prefix = "/forged";
  ResolvedRequest result;
  Q4T_CHECK(policy.Resolve(fields, Address::Parse("192.0.2.2"), "/v1/models",
                           false, &result));
  Q4T_CHECK(Same(result.source, "192.0.2.2") && result.host == "local");
  Q4T_CHECK(!result.proxied && result.scheme == "http");
  Q4T_CHECK(result.target == "/v1/models");
  return true;
}

Q4T_TEST(ingress_proxy_chain_stops_at_first_untrusted_source) {
  HttpPolicy policy;
  Q4T_CHECK(policy.Load(Policy()));
  HttpRequestHeaders fields;
  fields.host = "internal";
  fields.forwarded =
      "for=198.51.100.1;host=forged;proto=http, "
      "for=203.0.113.9;host=public.example;proto=https, "
      "for=10.0.0.2;host=internal";
  ResolvedRequest result;
  Q4T_CHECK(policy.Resolve(fields, Address::Parse("127.0.0.1"), "/v1/models",
                           false, &result));
  Q4T_CHECK(Same(result.source, "203.0.113.9"));
  Q4T_CHECK(result.host == "public.example" && result.scheme == "https");
  fields.forwarded = "for=10.0.0.3, for=10.0.0.2";
  Q4T_CHECK(policy.Resolve(fields, Address::Parse("127.0.0.1"), "/v1/models",
                           true, &result));
  Q4T_CHECK(Same(result.source, "10.0.0.3") && result.scheme == "https");
  return true;
}

Q4T_TEST(ingress_proxy_ipv6_and_port_syntax) {
  HttpPolicy policy;
  Q4T_CHECK(policy.Load(Policy()));
  HttpRequestHeaders fields;
  fields.host = "internal";
  fields.forwarded =
      "for=\"[2001:db8::1]:443\";host=\"public.example:443\";"
      "proto=https, for=\"[fd00::2]\"";
  ResolvedRequest result;
  Q4T_CHECK(policy.Resolve(fields, Address::Parse("::1"), "/v1/models", false,
                           &result));
  Q4T_CHECK(Same(result.source, "2001:db8::1") &&
            result.host == "public.example:443");
  for (const auto bad : {"unknown", "_hidden", "[2001:db8::1]:0",
                         "192.0.2.1:65536", "[fd00::2%eth0]", "1.2.3.4:bad"}) {
    fields.forwarded = "for=\"" + std::string(bad) + "\"";
    Q4T_CHECK(
        !policy.Resolve(fields, Address::Parse("::1"), "/", false, &result));
  }
  return true;
}

Q4T_TEST(ingress_xforwarded_parallel_lists_and_safe_singletons) {
  HttpPolicy policy;
  Q4T_CHECK(policy.Load(Policy("x-forwarded")));
  HttpRequestHeaders fields;
  fields.host = "internal";
  fields.forwarded_for = "198.51.100.9, 203.0.113.9, 10.0.0.2";
  fields.forwarded_host = "forged, public.example, internal";
  fields.forwarded_proto = "http, https, http";
  ResolvedRequest result;
  Q4T_CHECK(
      policy.Resolve(fields, Address::Parse("127.0.0.1"), "/", false, &result));
  Q4T_CHECK(Same(result.source, "203.0.113.9") &&
            result.host == "public.example" && result.scheme == "https");
  fields.forwarded_host = "public.example";
  fields.forwarded_proto = "https";
  Q4T_CHECK(
      policy.Resolve(fields, Address::Parse("127.0.0.1"), "/", false, &result));
  Q4T_CHECK(result.host == "public.example" && result.scheme == "https");
  fields.forwarded_host = "one,two";
  Q4T_CHECK(!policy.Resolve(fields, Address::Parse("127.0.0.1"), "/", false,
                            &result));
  return true;
}

Q4T_TEST(ingress_proxy_invalid_or_ambiguous_chain_never_falls_back) {
  HttpPolicy policy;
  Q4T_CHECK(policy.Load(Policy()));
  HttpRequestHeaders fields;
  fields.host = "internal";
  ResolvedRequest result;
  for (const auto bad :
       {"for=192.0.2.1;for=10.0.0.1", "for=\"192.0.2.1", "for=192.0.2.1,",
        "for=192.0.2.1;;host=x", "host=public.example",
        "for=192.0.2.1;proto=file", "for=192.0.2.1;host=evil:443"}) {
    fields.forwarded = bad;
    Q4T_CHECK(!policy.Resolve(fields, Address::Parse("127.0.0.1"), "/", false,
                              &result));
  }
  fields.forwarded.clear();
  for (int i = 0; i < 33; ++i)
    fields.forwarded += (i ? "," : "") + std::string("for=10.0.0.1");
  Q4T_CHECK(!policy.Resolve(fields, Address::Parse("127.0.0.1"), "/", false,
                            &result));
  return true;
}

Q4T_TEST(ingress_prefix_preserves_encoding_and_requires_stripping_authority) {
  HttpPolicy policy;
  Q4T_CHECK(policy.Load(Policy("forwarded", "/llm", true)));
  HttpRequestHeaders fields;
  fields.host = "public.example";
  const std::string tail = "/v1/models/org%2Fname?include%5B%5D=x";
  ResolvedRequest result;
  Q4T_CHECK(policy.Resolve(fields, Address::Parse("192.0.2.9"), "/llm" + tail,
                           true, &result) &&
            result.target == tail);
  Q4T_CHECK(!policy.Resolve(fields, Address::Parse("192.0.2.9"), tail, true,
                            &result));
  fields.forwarded_prefix = "/llm";
  Q4T_CHECK(!policy.Resolve(fields, Address::Parse("192.0.2.9"), tail, true,
                            &result));
  Q4T_CHECK(!policy.Resolve(fields, Address::Parse("127.0.0.1"), tail, true,
                            &result));
  fields.forwarded = "for=192.0.2.9";
  Q4T_CHECK(
      policy.Resolve(fields, Address::Parse("127.0.0.1"), tail, true, &result));
  Q4T_CHECK(result.target == tail && result.proxied);
  Q4T_CHECK(policy.Resolve(fields, Address::Parse("127.0.0.1"),
                           "/llm/llm" + tail, true, &result) &&
            result.target == "/llm" + tail);
  fields.forwarded_prefix = "/other";
  Q4T_CHECK(!policy.Resolve(fields, Address::Parse("127.0.0.1"), tail, true,
                            &result));
  return true;
}

Q4T_TEST(ingress_forwarding_headers_have_bounded_unambiguous_storage) {
  const auto parse = [](const std::string& lines, HttpRequestHeaders* fields) {
    const std::string wire =
        "GET /v1/models HTTP/1.1\r\nHost: local\r\n" + lines + "\r\n";
    size_t offset = 0;
    std::string method, path, body;
    return q4t::server::ReadHttpRequest(
        -1, &method, &path, &body, 1000, nullptr, nullptr, fields, {},
        [&](void* data, size_t size, auto) -> ssize_t {
          size = std::min(size, wire.size() - offset);
          std::memcpy(data, wire.data() + offset, size);
          offset += size;
          return size;
        });
  };
  HttpRequestHeaders fields;
  Q4T_CHECK(parse("Forwarded: for=192.0.2.1\r\nForwarded: for=10.0.0.2\r\n",
                  &fields));
  Q4T_CHECK(fields.forwarded == "for=192.0.2.1,for=10.0.0.2");
  Q4T_CHECK(parse("X-Forwarded-For: 192.0.2.1\r\nX-Forwarded-For: 10.0.0.2\r\n",
                  &fields));
  Q4T_CHECK(fields.forwarded_for == "192.0.2.1,10.0.0.2");
  for (const auto field :
       {"X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-Prefix"})
    Q4T_CHECK(
        !parse(std::string(field) + ": a\r\n" + field + ": b\r\n", &fields));
  Q4T_CHECK(!parse("Forwarded: " + std::string(8193, 'x') + "\r\n", &fields));
  Q4T_CHECK(
      !parse("Connection: Forwarded\r\nForwarded: for=192.0.2.1\r\n", &fields));
  return true;
}

int main(int argc, char** argv) {
  if (argc == 1) return q4t::test::RunAllTests();
  if (argc >= 3 && std::string_view(argv[1]) == "--tests")
    return q4t::test::RunTests(std::vector<std::string>(argv + 2, argv + argc));
  if (argc != 2 || std::string_view(argv[1]) != "--check-http") return 2;
  std::string raw((std::istreambuf_iterator<char>(std::cin)), {});
  Json value;
  HttpPolicy policy;
  return raw.size() <= 65536 && w::Parse(raw, &value) && policy.Load(value) ? 0
                                                                            : 1;
}
