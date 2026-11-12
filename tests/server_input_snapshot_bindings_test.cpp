#include "q4t/server/input_snapshot_bindings.h"

#include <algorithm>
#include <cstdlib>
#include <filesystem>

#include "../src/server/object_protocol.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/responses_state.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = fs::absolute(out ? out : ".q4t-work/input-binding-cpu");
    fs::create_directories(root);
    auto path = (root / "binding-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("binding fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
StateAccess Access(std::string scope = "client:bindings") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
BinaryAccess Binary() {
  auto access = Access();
  return {access.scope, access.deadline, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
Json Part() {
  return Object({{"type", String("input_file")},
                 {"file_url", String("https://example.com/frozen.txt")}});
}
Json Item(std::string id = "msg_file") {
  return Object({{"type", String("message")},
                 {"id", String(id)},
                 {"role", String("user")},
                 {"status", String("completed")},
                 {"content", Array({Part()})}});
}
Json NewInput() {
  return Object({{"type", String("message")},
                 {"role", String("user")},
                 {"content", Array({Part()})}});
}
ResponsesEnvelope Envelope(Json input, std::string previous = {},
                           std::string conversation = {}) {
  auto request =
      Object({{"model", String("cpu")}, {"input", std::move(input)}});
  if (!previous.empty())
    Set(&request, "previous_response_id", String(previous));
  if (!conversation.empty())
    Set(&request, "conversation", String(conversation));
  ChatPlanConfig config;
  config.model = "cpu";
  ResponsesEnvelope envelope;
  ChatPlanError error;
  auto status = ParseResponsesEnvelope(request, config, &envelope, &error);
  if (!status) throw std::runtime_error(error.param + ": " + status.message());
  return envelope;
}
Json Response(const ResponseStatePlan& plan, const char* status) {
  return Object({{"id", String(plan.id)},
                 {"object", String("response")},
                 {"model", String("cpu")},
                 {"status", String(status)},
                 {"output", Array()}});
}
ObjectReply Get(ObjectStore& objects, ObjectKind kind, const std::string& id) {
  ObjectRequest request;
  request.kind = kind;
  request.id = id;
  request.scope = Access().scope;
  return objects.Execute(request, Access().deadline);
}
ObjectReply Attach(InputSnapshotStore& snapshots, ResponseStatePlan* plan,
                   InputSnapshotRef* reference, InputSnapshotHold* hold) {
  const auto& item = plan->history.array[0];
  const auto& part = item.GetArray("content")->array[0];
  std::string identity;
  if (!InputValueIdentity(part, &identity))
    throw std::runtime_error("identity");
  const std::string_view raw("original resolved content");
  auto result = snapshots.Save(Binary(), identity, Object(), std::span(&raw, 1),
                               {}, 60, reference, hold);
  if (!result.ok()) return result;
  Json binding;
  result = BindInputSnapshot(item, "/content/0", *reference, &binding);
  if (!result.ok()) return result;
  return MergeInputSnapshots(Array({binding}), plan->history,
                             &plan->input_snapshots);
}
bool Equal(const Json& a, const Json& b) {
  return CompareStoredItem(a, b).ok();
}
}  // namespace

Q4T_TEST(
    input_binding_structural_identity_preserves_exact_numbers_and_large_data) {
  Json a, b;
  Q4T_CHECK(Parse(R"({"b":[1,9007199254740993],"a":"x"})", &a) &&
            Parse(R"({"a":"x","b":[1.0,9007199254740993.0]})", &b));
  Q4T_CHECK(Equal(a, b));
  Field(b, "b").array[1].str = "9007199254740992";
  Q4T_CHECK(!Equal(a, b));
  a = Object(
      {{"payload", String(std::string(9 << 20, 'x'))}, {"part", Part()}});
  b = a;
  std::reverse(b.object.begin(), b.object.end());
  Q4T_CHECK(Equal(a, b));
  Field(b, "payload").str.back() = 'y';
  Q4T_CHECK(!Equal(a, b));
  auto duplicate = Object({{"same", Number(1)}, {"same", Number(1)}});
  std::string hash = "must clear";
  Q4T_CHECK(!InputValueIdentity(duplicate, &hash) && hash.empty());
  return true;
}

Q4T_TEST(input_binding_paths_identity_conflicts_and_atomic_subsets) {
  const InputSnapshotRef reference{"input_snapshot_original", Digest("root")};
  auto item = Item();
  Json binding;
  Q4T_CHECK(Ok(BindInputSnapshot(item, "/content/0", reference, &binding)));
  auto all = Array({item});
  Json bindings;
  Q4T_CHECK(Ok(MergeInputSnapshots(Array({binding}), all, &bindings)));
  Q4T_CHECK(Ok(MergeInputSnapshots(Array({binding}), all, &bindings)) &&
            bindings.array.size() == 1);
  for (const auto* path :
       {"/metadata/0", "/content/00", "/content/1", "/content"}) {
    Json bad;
    Q4T_CHECK(!BindInputSnapshot(item, path, reference, &bad).ok());
  }
  Json conflicting;
  Q4T_CHECK(Ok(BindInputSnapshot(item, "/content/0",
                                 {"input_snapshot_other", Digest("other")},
                                 &conflicting)));
  const auto before = bindings;
  Q4T_CHECK(
      MergeInputSnapshots(Array({conflicting}), all, &bindings).http_status ==
          409 &&
      Equal(before, bindings));
  Set(&Field(item, "content").array[0], "file_url",
      String("https://example.com/changed"));
  Q4T_CHECK(!MergeInputSnapshots(Json(), Array({item}), &bindings).ok() &&
            Equal(before, bindings));
  Json selected;
  Q4T_CHECK(Ok(SelectInputSnapshots(bindings, Array({Item("different")}),
                                    &selected)) &&
            selected.array.empty());
  auto tool = Object({{"id", String("output_tool")},
                      {"type", String("function_call_output")},
                      {"output", Array({Part()})}});
  Q4T_CHECK(Ok(BindInputSnapshot(tool, "/output/0", reference, &binding)));
  return true;
}

Q4T_TEST(
    input_binding_response_previous_and_item_reference_keep_exact_snapshot) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesState states(objects);
  InputSnapshotStore snapshots(objects);
  ResponseStatePlan first;
  Q4T_CHECK(
      Ok(states.Resolve(Envelope(Array({NewInput()})), Access(), &first)));
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(
      Ok(Attach(snapshots, &first, &reference, &hold)) &&
      Ok(states.Begin(Response(first, "in_progress"), Access(), &first)) &&
      Ok(snapshots.Release(Binary(), &hold)) &&
      Ok(states.Finish(Response(first, "completed"), Array(), Access(),
                       &first)));
  auto public_response = states.Retrieve(first.id, Access());
  Q4T_CHECK(Ok(public_response) &&
            !public_response.value.Has("input_snapshots"));
  ResponseStatePlan previous;
  Q4T_CHECK(Ok(states.Resolve(Envelope(String("follow up"), first.id), Access(),
                              &previous)) &&
            Equal(previous.input_snapshots, first.input_snapshots));
  ResponseStatePlan item;
  Q4T_CHECK(
      Ok(states.Resolve(
          Envelope(Array({Object(
              {{"type", String("item_reference")},
               {"id", String(first.own_input.array[0].GetString("id"))}})})),
          Access(), &item)) &&
      Equal(item.input_snapshots, first.input_snapshots));
  Q4T_CHECK(Ok(states.Erase(first.id, Access())));
  InputSnapshot restored;
  Q4T_CHECK(Ok(snapshots.Read(
                Binary(), reference,
                first.input_snapshots.array[0].GetString("binding_sha256"), {},
                &restored)) &&
            restored.parts[0] == "original resolved content");
  return true;
}

Q4T_TEST(
    input_binding_multiple_references_and_queued_restart_restore_privately) {
  Fixture fixture;
  std::string id;
  Json expected;
  {
    ObjectStore objects(fixture.options);
    ResponsesState states(objects);
    InputSnapshotStore snapshots(objects);
    auto refs = Array();
    for (int i = 0; i < 2; ++i) {
      ResponseStatePlan plan;
      Q4T_CHECK(
          Ok(states.Resolve(Envelope(Array({NewInput()})), Access(), &plan)));
      InputSnapshotRef reference;
      InputSnapshotHold hold;
      Q4T_CHECK(
          Ok(Attach(snapshots, &plan, &reference, &hold)) &&
          Ok(states.Begin(Response(plan, "in_progress"), Access(), &plan)) &&
          Ok(states.Finish(Response(plan, "completed"), Array(), Access(),
                           &plan)) &&
          Ok(snapshots.Release(Binary(), &hold)));
      refs.array.push_back(
          Object({{"type", String("item_reference")},
                  {"id", String(plan.own_input.array[0].GetString("id"))}}));
    }
    ResponseStatePlan queued;
    Q4T_CHECK(Ok(states.Resolve(Envelope(refs), Access(), &queued)) &&
              queued.input_snapshots.array.size() == 2);
    Q4T_CHECK(Ok(states.Begin(Response(queued, "queued"), Access(), &queued)));
    id = queued.id;
    expected = queued.input_snapshots;
  }
  ObjectStore objects(fixture.options);
  ResponsesState states(objects);
  ResponseStatePlan restored;
  Q4T_CHECK(Ok(states.LoadPlan(id, Access(), &restored)) &&
            Equal(restored.input_snapshots, expected));
  Q4T_CHECK(states.LoadPlan(id, Access("other"), &restored).http_status == 404);
  return true;
}

Q4T_TEST(input_binding_first_use_conversation_items_survive_copy_and_delete) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesState states(objects);
  ConversationState conversations(objects);
  InputSnapshotStore snapshots(objects);
  const auto created =
      conversations.Create(Object(), Array({Item()}), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  ResponseStatePlan plan;
  Q4T_CHECK(Ok(states.Resolve(Envelope(String("read the file"), {}, id),
                              Access(), &plan)));
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Attach(snapshots, &plan, &reference, &hold)) &&
            Ok(states.Begin(Response(plan, "in_progress"), Access(), &plan)) &&
            Ok(states.Finish(Response(plan, "completed"), Array(), Access(),
                             &plan)) &&
            Ok(snapshots.Release(Binary(), &hold)));
  const auto stored = Get(objects, ObjectKind::kConversation, id);
  Q4T_CHECK(Ok(stored) &&
            stored.value.Find("data")->GetArray("items")->array.size() == 2 &&
            Equal(*stored.value.Find("data")->Find("input_snapshots"),
                  plan.input_snapshots));
  const auto independently_stored =
      Get(objects, ObjectKind::kConversationItem, "msg_file");
  Q4T_CHECK(
      Ok(independently_stored) &&
      Equal(*independently_stored.value.Find("data")->Find("input_snapshots"),
            plan.input_snapshots));
  const auto copied = conversations.Create(Object(), Array({Item()}), Access());
  Q4T_CHECK(Ok(copied));
  const auto copy =
      Get(objects, ObjectKind::kConversation, copied.value.GetString("id"));
  Q4T_CHECK(Ok(copy) && Equal(*copy.value.Find("data")->Find("input_snapshots"),
                              plan.input_snapshots));
  Q4T_CHECK(Ok(conversations.EraseItem(id, "msg_file", Access())));
  const auto removed = Get(objects, ObjectKind::kConversation, id);
  Q4T_CHECK(
      Ok(removed) &&
      removed.value.Find("data")->GetArray("input_snapshots")->array.empty());
  auto listed =
      conversations.ListItems(copied.value.GetString("id"), {}, Access());
  Q4T_CHECK(Ok(listed) &&
            !listed.value.GetArray("data")->array[0].Has("input_snapshots"));
  return true;
}

Q4T_TEST(input_binding_changed_original_is_rejected_before_begin) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesState states(objects);
  InputSnapshotStore snapshots(objects);
  ResponseStatePlan plan;
  Q4T_CHECK(Ok(states.Resolve(Envelope(Array({NewInput()})), Access(), &plan)));
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Attach(snapshots, &plan, &reference, &hold)));
  Set(&Field(plan.history.array[0], "content").array[0], "file_url",
      String("https://example.com/different"));
  Q4T_CHECK(states.Begin(Response(plan, "in_progress"), Access(), &plan).code ==
                "input_snapshot_binding_invalid" &&
            plan.response_revision == 0);
  Q4T_CHECK(states.Retrieve(plan.id, Access()).http_status == 404);
  Q4T_CHECK(Ok(snapshots.Release(Binary(), &hold)));
  return true;
}

Q4T_TEST(input_binding_explicit_tool_output_preserves_immutable_file) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesState states(objects);
  InputSnapshotStore snapshots(objects);
  auto tool = Object({{"type", String("function_call_output")},
                      {"id", String("output_original")},
                      {"call_id", String("call_original")},
                      {"output", Array({Part()})}});
  ResponseStatePlan plan;
  Q4T_CHECK(Ok(states.Resolve(Envelope(Array({tool})), Access(), &plan)));
  const auto& original = plan.history.array[0];
  std::string identity;
  Q4T_CHECK(
      InputValueIdentity(original.GetArray("output")->array[0], &identity));
  const std::string_view raw("resolved tool file");
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(snapshots.Save(Binary(), identity, Object(), std::span(&raw, 1),
                              {}, 60, &reference, &hold)));
  Json binding;
  Q4T_CHECK(Ok(BindInputSnapshot(original, "/output/0", reference, &binding)) &&
            Ok(MergeInputSnapshots(Array({binding}), plan.history,
                                   &plan.input_snapshots)) &&
            Ok(states.Begin(Response(plan, "in_progress"), Access(), &plan)) &&
            Ok(states.Finish(Response(plan, "completed"), Array(), Access(),
                             &plan)) &&
            Ok(snapshots.Release(Binary(), &hold)));
  ResponseStatePlan next;
  Q4T_CHECK(Ok(states.Resolve(Envelope(plan.own_input), Access(), &next)) &&
            Equal(next.input_snapshots, plan.input_snapshots));
  auto changed = plan.own_input;
  Set(&Field(changed.array[0], "output").array[0], "file_url",
      String("https://example.com/changed"));
  Q4T_CHECK(states.Resolve(Envelope(changed), Access(), &next).code ==
            "item_identity_conflict");
  return true;
}
