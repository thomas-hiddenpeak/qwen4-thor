#include "q4t/server/conversation_state.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>

#include "q4t/server/output_schema.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
namespace fs = std::filesystem;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("fixture JSON");
  return value;
}
Json String(std::string text) {
  Json value;
  value.type = Json::Type::kString;
  value.str = std::move(text);
  return value;
}
void Set(Json* value, std::string name, Json item) {
  for (auto& [key, current] : value->object) {
    if (key == name) {
      current = std::move(item);
      return;
    }
  }
  value->object.emplace_back(std::move(name), std::move(item));
}
Json Items(std::initializer_list<std::string> ids) {
  auto items = Parse("[]");
  for (const auto& id : ids) {
    auto item = Parse(R"({"type":"message","role":"user","status":"completed",
                          "content":[{"type":"input_text","text":"hello"}]})");
    Set(&item, "id", String(id));
    items.array.push_back(std::move(item));
  }
  return items;
}
StateAccess Access(std::string scope = "client:a") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
struct Fixture {
  fs::path directory;
  ObjectStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto root =
        fs::absolute(output ? output : ".q4t-work/conversations-host");
    fs::create_directories(root);
    auto name = (root / "conversation-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("fixture directory");
    directory = name;
    options.directory = (directory / "store").string();
    options.restart_backoff_ms = 0;
  }
};
ObjectReply ItemRecord(ObjectStore& store, std::string id) {
  ObjectRequest request;
  request.kind = ObjectKind::kConversationItem;
  request.scope = "client:a";
  request.id = std::move(id);
  return store.Execute(request, Clock::now() + 5s);
}
}  // namespace

Q4T_TEST(conversation_state_unknown_create_exposes_retrievable_identity) {
  ObjectWorkerEntryForTesting("--q4t-object-worker-commit-pause");
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  Fixture f;
  f.options.io_timeout_ms = 200;
  ObjectStore objects(f.options);
  ConversationState states(objects);
  const auto created = states.Create(Json(), Parse("[]"), Access());
  Q4T_CHECK(!created.ok() && created.commit_unknown);
  const auto id = created.value.GetString("id");
  Q4T_CHECK(!id.empty());
  const auto error = Parse(detail::ErrorJson(
      created.http_status, created.ClientMessage(), created.ClientCode()));
  Q4T_CHECK(error.Find("error")->GetString("code") == "object_commit_unknown");
  Q4T_CHECK(error.Find("error")->GetString("message").find(id) !=
            std::string::npos);
  const auto confirmed = states.Retrieve(id, Access());
  Q4T_CHECK(Ok(confirmed) && confirmed.value.GetString("id") == id);
  return true;
}

Q4T_TEST(conversation_state_crud_restart_and_item_retention) {
  Fixture f;
  std::string id;
  {
    ObjectStore objects(f.options);
    ConversationState states(objects);
    auto created = states.Create(Parse(R"({"topic":"project"})"),
                                 Items({"msg_1", "msg_2"}), Access());
    Q4T_CHECK(Ok(created));
    id = created.value.GetString("id");
    Q4T_CHECK(created.value.GetString("object") == "conversation");
    Q4T_CHECK(Ok(states.Update(id, Parse(R"({"topic":"updated"})"), Access())));
    Q4T_CHECK(ItemRecord(objects, "msg_1").value.GetInt("expires_at", -1) == 0);
  }
  ObjectStore objects(f.options);
  ConversationState states(objects);
  auto restored = states.Retrieve(id, Access());
  Q4T_CHECK(Ok(restored));
  Q4T_CHECK(restored.value.Find("metadata")->GetString("topic") == "updated");
  auto page = states.ListItems(id, {}, Access());
  Q4T_CHECK(Ok(page) && page.value.GetArray("data")->array.size() == 2);
  Q4T_CHECK(page.value.GetString("first_id") == "msg_2");
  Q4T_CHECK(Ok(states.Update(id, Json(), Access())));
  Q4T_CHECK(
      states.Retrieve(id, Access()).value.Find("metadata")->object.empty());
  auto erased = states.Erase(id, Access());
  Q4T_CHECK(Ok(erased) && erased.value.GetBool("deleted"));
  Q4T_CHECK(erased.value.GetString("object") == "conversation.deleted");
  Q4T_CHECK(states.Retrieve(id, Access()).http_status == 404);
  Q4T_CHECK(Ok(ItemRecord(objects, "msg_1")));
  Q4T_CHECK(Ok(ItemRecord(objects, "msg_2")));
  return true;
}

Q4T_TEST(conversation_state_item_identity_ignores_json_object_field_order) {
  Fixture f;
  ObjectStore objects(f.options);
  ConversationState state(objects);
  auto items = Items({"msg_shared"});
  const auto original = state.Create(Json(), items, Access());
  Q4T_CHECK(Ok(original));
  auto reordered = Parse(R"([{"content":[{"text":"hello","type":"input_text"}],
    "status":"completed","id":"msg_shared","role":"user","type":"message"}])");
  const auto shared = state.Create(Json(), reordered, Access());
  Q4T_CHECK(Ok(shared));
  Q4T_CHECK(shared.value.GetString("id") != original.value.GetString("id"));
  const auto saved = ItemRecord(objects, "msg_shared");
  Q4T_CHECK(Ok(saved) && saved.value.GetInt("revision") == 2);
  Set(&reordered.array[0], "role", String("assistant"));
  Q4T_CHECK(state.Create(Json(), reordered, Access()).code ==
            "item_identity_conflict");
  Q4T_CHECK(ItemRecord(objects, "msg_shared").value.GetInt("revision") == 2);
  return true;
}

Q4T_TEST(conversation_state_pending_tool_and_later_result) {
  Fixture f;
  ObjectStore objects(f.options);
  ConversationState states(objects);
  auto call = Parse(R"([{"type":"function_call","id":"fc_query",
    "call_id":"call_query","name":"lookup","arguments":"{",
    "status":"in_progress"}])");
  auto created = states.Create(Json(), call, Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  auto result = Parse(R"([{"type":"function_call_output","id":"fco_query",
    "call_id":"call_query","output":"source","status":"completed"}])");
  auto appended = states.Append(id, result, {}, Access());
  Q4T_CHECK(Ok(appended) &&
            appended.value.GetString("first_id") == "fco_query");
  Q4T_CHECK(!appended.value.GetBool("has_more"));
  ItemViewOptions options;
  options.descending = false;
  auto page = states.ListItems(id, options, Access());
  Q4T_CHECK(Ok(page) && page.value.GetArray("data")->array.size() == 2);
  Q4T_CHECK(page.value.GetArray("data")->array[0].GetString("arguments") ==
            "{");
  Q4T_CHECK(page.value.GetArray("data")->array[1].GetString("output") ==
            "source");
  // State storage does not pretend that incomplete arguments are executable.
  return true;
}

Q4T_TEST(conversation_state_client_and_membership_isolation) {
  Fixture f;
  ObjectStore objects(f.options);
  ConversationState states(objects);
  auto first = states.Create(Json(), Items({"msg_1"}), Access());
  auto second = states.Create(Json(), Items({"msg_2"}), Access());
  Q4T_CHECK(Ok(first) && Ok(second));
  const auto id = first.value.GetString("id");
  Q4T_CHECK(states.Retrieve(id, Access("client:b")).http_status == 404);
  Q4T_CHECK(states.Update(id, Json(), Access("client:b")).http_status == 404);
  Q4T_CHECK(states.Erase(id, Access("client:b")).http_status == 404);
  Q4T_CHECK(states.ListItems(id, {}, Access("client:b")).http_status == 404);
  Q4T_CHECK(
      states.Append(id, Items({"msg_3"}), {}, Access("client:b")).http_status ==
      404);
  Q4T_CHECK(states.RetrieveItem(id, "msg_2", {}, Access()).http_status == 404);
  Q4T_CHECK(states.EraseItem(id, "msg_2", Access()).http_status == 404);
  ItemViewOptions options;
  options.after = "msg_2";
  Q4T_CHECK(states.ListItems(id, options, Access()).http_status == 400);
  Q4T_CHECK(ItemRecord(objects, "msg_3").http_status == 404);
  return true;
}

Q4T_TEST(conversation_state_ordered_pagination_and_membership_delete) {
  Fixture f;
  ObjectStore objects(f.options);
  ConversationState states(objects);
  auto created =
      states.Create(Json(), Items({"msg_z", "msg_a", "msg_m"}), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  ItemViewOptions options;
  options.limit = 2;
  auto page = states.ListItems(id, options, Access());
  Q4T_CHECK(Ok(page) && page.value.GetString("first_id") == "msg_m");
  Q4T_CHECK(page.value.GetString("last_id") == "msg_a" &&
            page.value.GetBool("has_more"));
  options.after = "msg_a";
  page = states.ListItems(id, options, Access());
  Q4T_CHECK(Ok(page) && page.value.GetString("first_id") == "msg_z");
  Q4T_CHECK(!page.value.GetBool("has_more"));
  options.descending = false;
  options.after = "msg_z";
  page = states.ListItems(id, options, Access());
  Q4T_CHECK(Ok(page) && page.value.GetString("first_id") == "msg_a");
  Q4T_CHECK(page.value.GetString("last_id") == "msg_m");
  options.after = "msg_m";
  page = states.ListItems(id, options, Access());
  Q4T_CHECK(Ok(page) && page.value.GetArray("data")->array.empty());
  Q4T_CHECK(page.value.GetString("first_id").empty() &&
            !page.value.GetBool("has_more"));
  auto removed = states.EraseItem(id, "msg_a", Access());
  Q4T_CHECK(Ok(removed) && removed.value.GetString("object") == "conversation");
  Q4T_CHECK(states.RetrieveItem(id, "msg_a", {}, Access()).http_status == 404);
  Q4T_CHECK(Ok(ItemRecord(objects, "msg_a")));
  Q4T_CHECK(
      states.ListItems(id, {}, Access()).value.GetArray("data")->array.size() ==
      2);
  return true;
}

Q4T_TEST(conversation_state_invalid_and_conflicting_items_are_atomic) {
  Fixture f;
  ObjectStore objects(f.options);
  ConversationState states(objects);
  auto created = states.Create(Json(), Items({"msg_1"}), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  auto invalid = Items({"msg_2", "msg_3"});
  Set(&invalid.array[1], "extra", String("invalid"));
  Q4T_CHECK(states.Append(id, invalid, {}, Access()).http_status == 400);
  Q4T_CHECK(ItemRecord(objects, "msg_2").http_status == 404);
  Q4T_CHECK(states.Append(id, Items({"msg_1"}), {}, Access()).http_status ==
            409);
  auto conflict = Items({"msg_1"});
  Set(&conflict.array[0], "role", String("developer"));
  Q4T_CHECK(states.Create(Json(), conflict, Access()).http_status == 409);
  Q4T_CHECK(
      states.ListItems(id, {}, Access()).value.GetArray("data")->array.size() ==
      1);
  auto too_many = Items({});
  for (int i = 0; i < 21; ++i)
    too_many.array.push_back(Items({"msg_" + std::to_string(i)}).array[0]);
  Q4T_CHECK(states.Append(id, too_many, {}, Access()).http_status == 400);
  return true;
}

Q4T_TEST(conversation_state_quota_cannot_publish_partial_conversation) {
  Fixture f;
  f.options.max_client_objects = 2;
  ObjectStore objects(f.options);
  ConversationState states(objects);
  Q4T_CHECK(states.Create(Json(), Items({"msg_1", "msg_2"}), Access()).code ==
            "object_quota_exceeded");
  Q4T_CHECK(ItemRecord(objects, "msg_1").http_status == 404);
  ObjectRequest list;
  list.operation = ObjectOperation::kList;
  list.kind = ObjectKind::kConversation;
  list.scope = "client:a";
  auto existing = objects.Execute(list, Clock::now() + 5s);
  Q4T_CHECK(Ok(existing) && existing.value.GetArray("data")->array.empty());
  Q4T_CHECK(Ok(states.Create(Json(), Items({"msg_1"}), Access())));
  return true;
}

Q4T_TEST(conversation_state_include_projection_preserves_full_history) {
  Fixture f;
  ObjectStore objects(f.options);
  ConversationState states(objects);
  auto items = Parse(R"([{"type":"reasoning","id":"rs_1","summary":[],
    "encrypted_content":"opaque"},{"type":"message","id":"msg_1",
    "role":"user","status":"completed","content":[{"type":"input_image",
    "detail":"auto","image_url":"data:image/png;base64,AQ=="}]}])");
  auto created = states.Create(Json(), items, Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  auto projected = states.RetrieveItem(id, "rs_1", {}, Access());
  Q4T_CHECK(Ok(projected) &&
            projected.value.Find("encrypted_content")->IsNull());
  projected = states.RetrieveItem(id, "msg_1", {}, Access());
  Q4T_CHECK(Ok(projected) && projected.value.GetArray("content")
                                 ->array[0]
                                 .Find("image_url")
                                 ->IsNull());
  ItemViewOptions options;
  options.include = {"reasoning.encrypted_content",
                     "message.input_image.image_url"};
  Q4T_CHECK(states.RetrieveItem(id, "rs_1", options, Access())
                .value.GetString("encrypted_content") == "opaque");
  Q4T_CHECK(ItemRecord(objects, "msg_1")
                .value.Find("data")
                ->Find("item")
                ->GetArray("content")
                ->array[0]
                .GetString("image_url") == "data:image/png;base64,AQ==");
  ResponsesEnvelope envelope;
  envelope.request = Parse("{}");
  envelope.conversation_id = id;
  ResponseStatePlan plan;
  ResponsesState responses(objects);
  Q4T_CHECK(Ok(responses.Resolve(envelope, Access(), &plan)));
  Q4T_CHECK(plan.history.array[0].GetString("encrypted_content") == "opaque");
  return true;
}

Q4T_TEST(conversation_state_metadata_and_query_contract) {
  Fixture f;
  ObjectStore objects(f.options);
  ConversationState states(objects);
  auto metadata = Parse("{}");
  std::string key, value;
  for (int i = 0; i < 64; ++i) key += "键";
  for (int i = 0; i < 512; ++i) value += "值";
  Set(&metadata, key, String(value));
  Q4T_CHECK(Ok(states.Create(metadata, Items({}), Access())));
  Set(&metadata, key, String(value + "值"));
  Q4T_CHECK(states.Create(metadata, Items({}), Access()).http_status == 400);
  Q4T_CHECK(
      states.Create(Parse(R"({"a":1})"), Items({}), Access()).http_status ==
      400);
  ItemViewOptions options;
  ChatPlanError error;
  Q4T_CHECK(ParseItemViewOptions(
      Parse(
          R"({"limit":2,"order":"asc","include":["file_search_call.results"]})"),
      &options, &error));
  Q4T_CHECK(options.limit == 2 && !options.descending &&
            options.include.size() == 1);
  for (
      const auto* text :
      {R"({"limit":0})", R"({"limit":101})", R"({"limit":2.5})",
       R"({"limit":"2"})", R"({"order":"wrong"})", R"({"after":"../x"})",
       R"({"include":["wrong"]})",
       R"({"include":["reasoning.encrypted_content","reasoning.encrypted_content"]})",
       R"({"before":"msg_1"})"})
    Q4T_CHECK(!ParseItemViewOptions(Parse(text), &options, &error));
  Q4T_CHECK(
      !ParseItemViewOptions(Parse(R"({"limit":2})"), &options, &error, false));
  return true;
}

Q4T_TEST(conversation_state_resolves_all_declared_input_variants) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState responses(objects);
  const auto path = fs::path(__FILE__).parent_path() /
                    "fixtures/responses_items/sdk_vectors.json";
  std::ifstream source(path);
  Q4T_CHECK(source.good());
  auto fixture = Parse(std::string{std::istreambuf_iterator<char>(source), {}});
  size_t resolved = 0, conversation_shapes = 0;
  for (const auto& vector : fixture.GetArray("vectors")->array) {
    if (vector.GetString("shape") != "input_item" ||
        !vector.GetBool("positive"))
      continue;
    const auto& input = *vector.Find("input");
    if (input.GetString("type") == "item_reference" ||
        (!input.Has("role") && input.GetString("type").empty()))
      continue;
    auto items = Parse("[]");
    items.array.push_back(input);
    if (input.GetString("type") == "compaction") {
      // The SDK vector's arbitrary string proves shape only. Runtime state
      // also requires our opaque identity envelope; cryptographic validation
      // remains in generation, not this shape-resolution test.
      Q4T_CHECK(responses.ResolveItems(items, Access()).code ==
                "invalid_encrypted_content");
      Set(&items.array[0], "encrypted_content",
          String("q4tc1:item_compaction_fixture:q4t1:shape-only"));
    }
    auto canonical = responses.ResolveItems(items, Access());
    Q4T_CHECK(Ok(canonical) && canonical.value.array.size() == 1);
    ++resolved;
    const auto& item = canonical.value.array.front();
    Q4T_CHECK(!item.GetString("id").empty());
    // This is an executor command, not a ConversationItem. The generation
    // integration must execute compaction before publishing its resulting item.
    if (item.GetString("type") == "compaction_trigger") continue;
    ChatPlanError error;
    const auto status = ValidateResponsesItemShape(
        item, ResponsesItemShape::kConversation, &error);
    if (!status)
      std::fprintf(stderr, "%s: %s %s\n", vector.GetString("name").c_str(),
                   error.param.c_str(), status.message().c_str());
    Q4T_CHECK(status);
    const auto projected = ProjectStoredItem(item, {});
    Q4T_CHECK(ValidateResponsesItemShape(
        projected, ResponsesItemShape::kConversation, &error));
    ++conversation_shapes;
  }
  Q4T_CHECK(resolved == 32 && conversation_shapes == 31);
  Q4T_CHECK(objects.Health().workers_started == 0);
  return true;
}

Q4T_TEST(conversation_state_optional_reference_discriminator_and_scope) {
  Fixture f;
  ObjectStore objects(f.options);
  ConversationState conversations(objects);
  ResponsesState responses(objects);
  Q4T_CHECK(Ok(conversations.Create(Json(), Items({"msg_1"}), Access())));
  for (const auto* text :
       {R"([{"id":"msg_1"}])", R"([{"id":"msg_1","type":null}])",
        R"([{"id":"msg_1","type":"item_reference"}])"}) {
    auto result = responses.ResolveItems(Parse(text), Access());
    Q4T_CHECK(Ok(result) &&
              result.value.array[0].GetString("type") == "message");
    Q4T_CHECK(
        responses.ResolveItems(Parse(text), Access("client:b")).http_status ==
        404);
  }
  auto call = responses.ResolveItems(Parse(R"([{"type":"function_call_output",
    "id":null,"call_id":"call_1","output":"{}"}])"),
                                     Access());
  Q4T_CHECK(Ok(call) && !call.value.array[0].GetString("id").empty());
  Q4T_CHECK(call.value.array[0].GetString("status") == "completed");
  return true;
}

Q4T_TEST(conversation_state_rejects_bad_input_before_reference_io) {
  Fixture f;
  f.options.directory = "/unconfigured/not-opened";
  ObjectStore objects(f.options);
  ResponsesState responses(objects);
  auto bad = Parse(R"([{"id":"msg_1"},{"type":"function_call",
    "call_id":"call_1","arguments":"{}"}])");
  auto result = responses.ResolveItems(bad, Access());
  Q4T_CHECK(result.http_status == 400);
  Q4T_CHECK(objects.Health().workers_started == 0);
  auto triggers = Parse(R"([{"type":"compaction_trigger"},
    {"role":"user","content":"next"}])");
  Q4T_CHECK(responses.ResolveItems(triggers, Access()).http_status == 400);
  Q4T_CHECK(objects.Health().workers_started == 0);
  return true;
}

Q4T_TEST(conversation_state_response_cannot_duplicate_membership) {
  Fixture f;
  ObjectStore objects(f.options);
  ConversationState conversations(objects);
  ResponsesState responses(objects);
  auto created = conversations.Create(Json(), Items({"msg_1"}), Access());
  Q4T_CHECK(Ok(created));
  ResponsesEnvelope envelope;
  envelope.request = Parse(R"({"input":[{"id":"msg_1"}]})");
  envelope.conversation_id = created.value.GetString("id");
  ResponseStatePlan plan;
  Q4T_CHECK(Ok(responses.Resolve(envelope, Access(), &plan)));
  auto snapshot = Parse(R"({"status":"in_progress","output":[]})");
  Set(&snapshot, "id", String(plan.id));
  Q4T_CHECK(Ok(responses.Begin(snapshot, Access(), &plan)));
  Set(&snapshot, "status", String("completed"));
  Q4T_CHECK(
      responses.Finish(snapshot, Parse("[]"), Access(), &plan).http_status ==
      409);
  Q4T_CHECK(responses.Retrieve(plan.id, Access()).value.GetString("status") ==
            "in_progress");
  Q4T_CHECK(conversations.ListItems(envelope.conversation_id, {}, Access())
                .value.GetArray("data")
                ->array.size() == 1);
  Set(&snapshot, "status", String("failed"));
  Q4T_CHECK(Ok(responses.Finish(snapshot, Parse("[]"), Access(), &plan)));
  return true;
}

Q4T_TEST(conversation_state_screenshot_absence_and_include_contract) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState responses(objects);
  ConversationState conversations(objects);
  const auto input =
      Parse(R"([{"type":"computer_call_output","call_id":"call_1",
    "id":null,"acknowledged_safety_checks":null,"output":{
    "type":"computer_screenshot","image_url":"data:image/png;base64,AQ=="}}])");
  auto resolved = responses.ResolveItems(input, Access());
  Q4T_CHECK(Ok(resolved));
  const auto& item = resolved.value.array[0];
  Q4T_CHECK(!item.Has("acknowledged_safety_checks"));
  Q4T_CHECK(!item.Find("output")->Has("file_id"));
  Q4T_CHECK(!item.Find("output")->Has("detail"));
  auto created = conversations.Create(Json(), resolved.value, Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  auto hidden =
      conversations.RetrieveItem(id, item.GetString("id"), {}, Access());
  Q4T_CHECK(Ok(hidden) && !hidden.value.Find("output")->Has("image_url"));
  ChatPlanError error;
  Q4T_CHECK(ValidateResponsesItemShape(
      hidden.value, ResponsesItemShape::kConversation, &error));
  ItemViewOptions options;
  options.include = {"computer_call_output.output.image_url"};
  auto full =
      conversations.RetrieveItem(id, item.GetString("id"), options, Access());
  Q4T_CHECK(Ok(full) && full.value.Find("output")->GetString("image_url") ==
                            "data:image/png;base64,AQ==");
  return true;
}
