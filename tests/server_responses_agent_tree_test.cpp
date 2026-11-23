#include "q4t/server/responses_agent_tree.h"

#include <algorithm>
#include <stdexcept>
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using q4t::server::ResponsesAgentTree;

Json Action(ResponsesAgentTree& tree, std::string_view caller,
            std::string_view action, const std::string& raw,
            bool success = true) {
  Json args, result;
  if (!q4t::io::ParseJson(raw, &args)) throw std::runtime_error("Bad fixture");
  const auto status = tree.Act(caller, action, args, &result);
  if (status.ok() != success)
    throw std::runtime_error(std::string(action) + ": " + status.message());
  return result;
}

Q4T_TEST(
    responses_agent_tree_capacity_covers_descendants_and_fences_late_work) {
  ResponsesAgentTree tree(2), other(2);
  Action(tree, "root", "spawn_agent", R"({"name":"a","task":"first"})");
  Action(other, "root", "wait_agent", R"({"target":"root/a"})", false);
  ResponsesAgentTree::Turn a, b, stale;
  Q4T_CHECK(tree.Claim("root/a", &a));
  Action(tree, a.name, "spawn_agent", R"({"name":"b","task":"second"})");
  Action(tree, "root", "spawn_agent", R"({"name":"c","task":"third"})", false);
  Q4T_CHECK(tree.Claim("root/a/b", &b));
  Q4T_CHECK(!tree.Claim("root/a/b", &stale));
  Action(tree, b.name, "send_message",
         R"({"target":"root","message":"found result"})");
  Q4T_CHECK(tree.RootMessages().array.size() == 1);
  Q4T_CHECK(tree.RootMessages().array.front().GetString("content").find(
                "root/a/b") != std::string::npos);
  Action(tree, b.name, "interrupt_agent", R"({"target":"root/a"})", false);
  Action(tree, "root", "interrupt_agent", R"({"target":"root/a"})");
  Q4T_CHECK(!tree.Finish(a, "late", true));
  Q4T_CHECK(!tree.Finish(b, "late", true));
  Action(tree, "root", "followup_task",
         R"({"target":"root/a","task":"again"})");
  Q4T_CHECK(tree.Claim("root/a", &stale));
  Q4T_CHECK(!tree.Finish(a, "stale generation", true));
  Q4T_CHECK(tree.Finish(stale, "actual result", true));
  const auto result =
      Action(tree, "root", "wait_agent", R"({"target":"root/a"})");
  Q4T_CHECK(result.GetString("output") == "actual result");
  Q4T_CHECK(result.GetString("status") == "completed");
  Action(tree, "root", "spawn_agent", R"({"name":"c","task":"third"})");
  return true;
}

Q4T_TEST(responses_agent_tree_orders_messages_received_during_a_model_turn) {
  ResponsesAgentTree tree(1);
  Action(tree, "root", "spawn_agent", R"({"name":"a","task":"first"})");
  ResponsesAgentTree::Turn turn;
  Q4T_CHECK(tree.Claim("root/a", &turn));
  Action(tree, "root", "send_message",
         R"({"target":"root/a","message":"later"})");
  Q4T_CHECK(tree.Finish(turn, "reply", true));
  Action(tree, "root", "followup_task", R"({"target":"root/a","task":"next"})");
  Q4T_CHECK(tree.Claim("root/a", &turn));
  Q4T_CHECK(turn.messages.array.size() == 4U);
  Q4T_CHECK(turn.messages.array[1].GetString("content") == "reply");
  Q4T_CHECK(turn.messages.array[2].GetString("content") == "later");
  Q4T_CHECK(tree.Finish(turn, "partial", false));
  const auto list = Action(tree, "root", "list_agents", "{}");
  Q4T_CHECK(list.Find("agents")->array[0].GetString("status") == "incomplete");
  Action(tree, "root", "spawn_agent", R"({"name":"../a","task":"outside"})",
         false);
  Action(tree, "root", "list_agents", R"({"caller":"other"})", false);
  return true;
}

Q4T_TEST(
    managed_subagent_close_and_resume_preserve_task_and_fence_stale_claims) {
  ResponsesAgentTree tree(2);
  const std::string target = R"({"target":"root/a"})";
  Action(tree, "root", "spawn_agent", R"({"name":"a","task":"record 456"})");
  ResponsesAgentTree::Turn first, nested, later;
  Q4T_CHECK(tree.Claim("root/a", &first));
  Action(tree, first.name, "spawn_agent", R"({"name":"b","task":"child"})");
  Q4T_CHECK(tree.Claim("root/a/b", &nested));
  Json args, result;
  Q4T_CHECK(q4t::io::ParseJson(target, &args));
  const auto reader = tree.ChildTools(nested.name);
  Q4T_CHECK(reader.array.size() == 2);
  for (const auto& t : reader.array) {
    const auto name = t.Find("function")->GetString("name");
    Q4T_CHECK(name == "multi_agent_list_agents" ||
              name == "multi_agent_send_message");
  }
  const auto can_interrupt = tree.ChildTools(first.name);
  Q4T_CHECK(std::any_of(can_interrupt.array.begin(), can_interrupt.array.end(),
                        [](const Json& t) {
                          return t.Find("function")->GetString("name") ==
                                 "multi_agent_interrupt_agent";
                        }));
  auto legacy = tree.SaveSession();
  for (auto& row : legacy.array) {
    std::erase_if(row.object,
                  [](const auto& key) { return key.first == "opened_at"; });
  }
  ResponsesAgentTree old(2);
  Q4T_CHECK(old.RestoreSession(legacy));
  Q4T_CHECK(!old.SetClosed("root", true, args, &result));
  Q4T_CHECK(old.SetClosed("root", true, args, &result, 123456));
  Q4T_CHECK(result.GetInt("opened_at") == 123456);
  Q4T_CHECK(!tree.SetClosed(nested.name, true, args, &result));
  Q4T_CHECK(tree.SetClosed("root", true, args, &result));
  const auto original = result.GetInt("opened_at");
  Q4T_CHECK(original > 0 && result.GetInt("closed_at") >= original);
  Q4T_CHECK(!tree.Finish(first, "late", true) &&
            !tree.Finish(nested, "late", true));
  Action(tree, "root", "followup_task",
         R"({"target":"root/a","task":"invalid"})", false);
  Q4T_CHECK(!tree.SetClosed("root", true, args, &result));
  ResponsesAgentTree restored(2);
  Q4T_CHECK(restored.RestoreSession(tree.SaveSession()));
  Q4T_CHECK(!restored.Claim(first.name, &later));
  Q4T_CHECK(restored.SetClosed("root", false, args, &result));
  Q4T_CHECK(result.GetString("status") == "active" &&
            result.Find("closed_at")->IsNull() &&
            result.GetInt("opened_at") == original &&
            !result.Has("instructions"));
  Q4T_CHECK(!restored.SetClosed("root", false, args, &result));
  Action(restored, "root", "followup_task",
         R"({"target":"root/a","task":"add one"})");
  Q4T_CHECK(restored.Claim(first.name, &later) &&
            later.messages.array.size() == 2 &&
            later.messages.array.front().GetString("content") == "record 456");
  Q4T_CHECK(!restored.Finish(first, "stale", true));
  Q4T_CHECK(restored.Finish(later, "457", true));
  Q4T_CHECK(ResponsesAgentTree::ModelTools().array.size() == 6 &&
            ResponsesAgentTree::ModelTools(true).array.size() == 8);
  Q4T_CHECK(
      ResponsesAgentTree::ActionName("multi_agent_close_subagent").empty() &&
      ResponsesAgentTree::ActionName("multi_agent_close_subagent", true) ==
          "close_subagent");
  return true;
}
}  // namespace
