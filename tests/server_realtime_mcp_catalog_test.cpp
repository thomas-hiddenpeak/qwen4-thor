#include "q4t/server/realtime_mcp_catalog.h"

#include "../src/server/object_protocol.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
Json Catalog() {
  return Object(
      {{"type", String("mcp_list_tools")},
       {"id", String("mcpl_project")},
       {"server_label", String("project")},
       {"tools",
        Array({Object(
            {{"name", String("read")},
             {"input_schema",
              Object({{"type", String("object")},
                      {"description", String("authorization data")}})},
             {"annotations", Object({{"readOnlyHint", Boolean(true)}})},
             {"description", String("Read real project records")}})})}});
}
}  // namespace

Q4T_TEST(realtime_mcp_catalog_projection_preserves_actual_tools_and_sdk_shape) {
  const auto source = Catalog();
  const auto before = OutputJson(source);
  Json projected;
  bool loaded = false;
  ChatPlanError error;
  Q4T_CHECK(ProjectRealtimeMcpCatalog(source, &projected, &loaded, &error));
  Q4T_CHECK(loaded && OutputJson(source) == before &&
            OutputJson(projected) == before);
  Q4T_CHECK(ValidateRealtimeShape(projected, RealtimeShape::kItem, &error));
  Q4T_CHECK(projected.GetArray("tools")
                ->array[0]
                .Find("input_schema")
                ->GetString("description") == "authorization data");
  return true;
}

Q4T_TEST(realtime_mcp_catalog_successful_empty_list_is_loaded) {
  auto source = Catalog();
  Set(&source, "tools", Array());
  Set(&source, "error", Json());
  Json projected;
  bool loaded = false;
  ChatPlanError error;
  Q4T_CHECK(ProjectRealtimeMcpCatalog(source, &projected, &loaded, &error));
  Q4T_CHECK(loaded && projected.GetArray("tools")->array.empty() &&
            !projected.Has("error"));
  return true;
}

Q4T_TEST(realtime_mcp_catalog_failure_uses_separate_event_state) {
  auto source = Catalog();
  Set(&source, "tools", Array());
  Set(&source, "error", String("MCP tools/list failed"));
  Json projected;
  bool loaded = true;
  ChatPlanError error;
  Q4T_CHECK(ProjectRealtimeMcpCatalog(source, &projected, &loaded, &error));
  Q4T_CHECK(!loaded && projected.GetArray("tools")->array.empty() &&
            !projected.Has("error"));
  Q4T_CHECK(source.GetString("error") == "MCP tools/list failed");
  Q4T_CHECK(ValidateRealtimeShape(projected, RealtimeShape::kItem, &error));
  return true;
}

Q4T_TEST(realtime_mcp_catalog_failure_never_grants_callables) {
  auto source = Catalog();
  Set(&source, "error", String("listing failed"));
  Json projected = String("unchanged");
  bool loaded = false;
  ChatPlanError error;
  Q4T_CHECK(!ProjectRealtimeMcpCatalog(source, &projected, &loaded, &error));
  Q4T_CHECK(projected.IsString() && projected.str == "unchanged" && !loaded);
  return true;
}

Q4T_TEST(
    realtime_mcp_catalog_invalid_identity_and_duplicate_fields_fail_closed) {
  auto source = Catalog();
  Json projected = String("unchanged");
  bool loaded = false;
  ChatPlanError error;
  Set(&source, "id", String("client_catalog"));
  Q4T_CHECK(!ProjectRealtimeMcpCatalog(source, &projected, &loaded, &error));
  source = Catalog();
  source.object.push_back(source.object.back());
  Q4T_CHECK(!ProjectRealtimeMcpCatalog(source, &projected, &loaded, &error));
  source = Object({{"type", String("mcp_approval_response")},
                   {"approval_request_id", String("mcpr_other")},
                   {"approve", Boolean(true)}});
  Q4T_CHECK(!ProjectRealtimeMcpCatalog(source, &projected, &loaded, &error));
  Q4T_CHECK(projected.str == "unchanged" && !loaded);
  return true;
}

Q4T_TEST(realtime_mcp_catalog_storage_failure_preserves_output) {
  auto source = Catalog();
  Set(&Field(source, "tools").array[0], "description",
      String(std::string(4ULL << 20, 'x')));
  Json projected = String("unchanged");
  bool loaded = false;
  ChatPlanError error;
  Q4T_CHECK(!ProjectRealtimeMcpCatalog(source, &projected, &loaded, &error));
  Q4T_CHECK(error.http_status == 413 && projected.str == "unchanged" &&
            !loaded);
  return true;
}

Q4T_TEST(realtime_mcp_catalog_private_fields_cannot_escape_through_projection) {
  for (const auto* field : {"authorization", "headers", "session"}) {
    auto source = Catalog();
    Set(&source, field, String("fixture-private-value"));
    Json projected = String("unchanged");
    bool loaded = false;
    ChatPlanError error;
    Q4T_CHECK(!ProjectRealtimeMcpCatalog(source, &projected, &loaded, &error));
    Q4T_CHECK(projected.str == "unchanged" && !loaded);
  }
  return true;
}

Q4T_TEST(realtime_mcp_catalog_remote_spelling_does_not_invent_valid_authority) {
  auto source = Catalog();
  Set(&source, "tools",
      Array({Object({{"name", String("read")},
                     {"inputSchema", Object({{"type", String("object")}})}})}));
  Json projected = String("unchanged");
  bool loaded = false;
  ChatPlanError error;
  Q4T_CHECK(!ProjectRealtimeMcpCatalog(source, &projected, &loaded, &error));
  Q4T_CHECK(projected.str == "unchanged" && !loaded);
  return true;
}
