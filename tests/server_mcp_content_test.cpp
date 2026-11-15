#include "q4t/server/mcp_content.h"

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
Json Value(const std::string& text) {
  Json result;
  if (!Parse(text, &result)) throw std::runtime_error("MCP content fixture");
  return result;
}
Json ToolResult(Json parts) { return Object({{"content", std::move(parts)}}); }
}  // namespace

Q4T_TEST(mcp_content_native_binary_is_not_restricted_to_image_bytes) {
  auto result = Value(R"({"content":[
    {"type":"text","text":""},
    {"type":"image","mimeType":"image/png","data":"cGl4ZWxz"},
    {"type":"audio","mimeType":"audio/wav","data":"UklGRg=="},
    {"type":"resource","resource":{"uri":"file:///report.pdf",
      "mimeType":"application/pdf","blob":"JVBERg=="}},
    {"type":"resource","resource":{"uri":"project:empty","text":""}},
    {"type":"resource_link","uri":"project:report","name":"report",
      "title":"Report","description":"read via MCP","size":0}],
    "structuredContent":{"nested":[1,2]},"isError":true,
    "_meta":{"source":"native"}})");
  const auto original = OutputJson(result);
  Q4T_CHECK(ValidateMcpToolResult(result));
  Q4T_CHECK(OutputJson(result) == original);
  // Valid binary does not claim that a later codec can decode these bytes.
  return true;
}

Q4T_TEST(mcp_content_rejects_noncanonical_payloads_without_decoding_media) {
  for (const auto* data :
       {"Zg", "Zh==", "Zm9=", "Z=g=", "=AAA", "====", "Zg==\n", "Zg__",
        "data:image/png;base64,Zg==", "https://example.test/pixel"}) {
    auto part = Object({{"type", String("audio")},
                        {"mimeType", String("audio/wav")},
                        {"data", String(data)}});
    Q4T_CHECK(!ValidateMcpToolResult(ToolResult(Array({part}))));
  }
  for (const auto* data : {"", "Zg==", "Zm8=", "Zm9v", "/w==", "//8="}) {
    auto part = Object({{"type", String("audio")},
                        {"mimeType", String("audio/wav")},
                        {"data", String(data)}});
    Q4T_CHECK(ValidateMcpToolResult(ToolResult(Array({part}))));
  }
  return true;
}

Q4T_TEST(mcp_content_resource_identity_and_metadata_are_typed) {
  const auto valid = Value(R"({"contents":[{"uri":"file:///private/report",
    "mimeType":"text/plain","text":"remote only",
    "_meta":{"version":1},"annotations":{"audience":["assistant","user"],
    "priority":0.5,"lastModified":"2026-11-16T00:00:00Z"}}]})");
  Q4T_CHECK(ValidateMcpResourcesResult(valid));
  for (const auto* uri : {"file:///private/report", "https://origin/a?b=c",
                          "custom+v1:item", "urn:uuid:123", "x:"})
    Q4T_CHECK(McpResourceUri(uri));
  for (const auto* uri : {"", "/private/report", "../report", "1x:item",
                          "bad_scheme:item", "https://bad host/a", "x:\n"})
    Q4T_CHECK(!McpResourceUri(uri));
  for (const auto* patch :
       {R"({"blob":"YQ=="})", R"({"text":7})", R"({"_meta":[]})",
        R"({"mimeType":"bad\r\nheader"})",
        R"({"annotations":{"priority":1.1}})",
        R"({"annotations":{"audience":["system"]}})",
        R"({"annotations":{"lastModified":0}})"}) {
    auto malformed = valid;
    auto& resource = Field(malformed, "contents").array.front();
    for (const auto& [key, value] : Value(patch).object)
      Set(&resource, key, value);
    Q4T_CHECK(!ValidateMcpResourcesResult(malformed));
  }
  return true;
}

Q4T_TEST(mcp_content_rejects_bad_variants_and_bounds_native_result) {
  for (const auto* part :
       {R"({"type":"text"})", R"({"type":"image","data":"YQ=="})",
        R"({"type":"resource","resource":{"uri":"x:a"}})",
        R"({"type":"resource_link","uri":"x:a"})",
        R"({"type":"resource_link","uri":"x:a","name":"a","size":-1})",
        R"({"type":"resource_link","uri":"x:a","name":"a","size":1.5})",
        R"({"type":"input_image","image_url":"file:///private"})"})
    Q4T_CHECK(!ValidateMcpToolResult(ToolResult(Array({Value(part)}))));
  for (const auto* envelope :
       {R"({"content":{}})", R"({"content":[],"isError":"true"})",
        R"({"content":[],"structuredContent":[]})",
        R"({"content":[],"_meta":null})"})
    Q4T_CHECK(!ValidateMcpToolResult(Value(envelope)));
  auto parts = Array();
  for (int i = 0; i < 257; ++i)
    parts.array.push_back(Value(R"({"type":"text","text":""})"));
  Q4T_CHECK(!ValidateMcpToolResult(ToolResult(parts)));
  parts.array.pop_back();
  Q4T_CHECK(ValidateMcpToolResult(ToolResult(parts)));
  Set(&parts.array.front(), "text", String(std::string(4U << 20, 'x')));
  Q4T_CHECK(!ValidateMcpToolResult(ToolResult(parts)));
  return true;
}
