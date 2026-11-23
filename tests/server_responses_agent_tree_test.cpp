#include "q4t/server/responses_agent_tree.h"

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
}  // namespace
