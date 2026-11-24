#include "q4t/server/agent_cron.h"

#include <chrono>

#include "q4t/test.h"

namespace q4t::server {
namespace {
using namespace std::chrono;

sys_time<minutes> At(int y, unsigned m, unsigned d, int h, int min) {
  return sys_days(year(y) / month(m) / day(d)) + hours(h) + minutes(min);
}

Q4T_TEST(agent_cron_fields_ranges_names_and_lists) {
  const auto schedule = AgentCron::Parse("0,30 9-17/2 * JAN,nov mon-Fri");
  Q4T_CHECK(schedule);
  const auto& utc = *locate_zone("UTC");
  Q4T_CHECK(schedule->Matches(At(2026, 11, 24, 15, 30), utc));
  Q4T_CHECK(!schedule->Matches(At(2026, 11, 24, 16, 30), utc));
  Q4T_CHECK(!schedule->Matches(At(2026, 11, 22, 15, 30), utc));
  return true;
}

Q4T_TEST(agent_cron_day_rules_and_large_steps) {
  const auto either = AgentCron::Parse("0 0 13 * 7");
  const auto stepped = AgentCron::Parse("*/2147483647 0 */2 * Tue");
  Q4T_CHECK(either);
  Q4T_CHECK(stepped);
  const auto& utc = *locate_zone("UTC");
  Q4T_CHECK(either->Matches(At(2026, 11, 13, 0, 0), utc));
  Q4T_CHECK(either->Matches(At(2026, 11, 22, 0, 0), utc));
  Q4T_CHECK(!either->Matches(At(2026, 11, 24, 0, 0), utc));
  Q4T_CHECK(!stepped->Matches(At(2026, 11, 24, 0, 0), utc));
  Q4T_CHECK(stepped->Matches(At(2026, 11, 17, 0, 0), utc));
  return true;
}

Q4T_TEST(agent_cron_local_clock_gap_and_fold) {
  const auto repeated = AgentCron::Parse("30 1 * * *");
  const auto skipped = AgentCron::Parse("30 2 * * *");
  Q4T_CHECK(repeated);
  Q4T_CHECK(skipped);
  const auto& zone = *locate_zone("America/New_York");
  Q4T_CHECK(repeated->Matches(At(2026, 11, 1, 5, 30), zone));
  Q4T_CHECK(repeated->Matches(At(2026, 11, 1, 6, 30), zone));
  // The spring-forward day runs from 05:00 UTC to 04:00 UTC the next day.
  for (int i = 0; i < 23 * 60; ++i)
    Q4T_CHECK(!skipped->Matches(At(2026, 3, 8, 5, 0) + minutes(i), zone));
  return true;
}

Q4T_TEST(agent_cron_rejects_invalid_fields) {
  for (auto invalid :
       {"", "* * * *", "* * * * * *", "60 * * * *", "1, * * * *", "0 0 0 * *",
        "0 0 * * 8", "* * * NOV-APR *", "*\n* * * *", "0 0 * Not Tue",
        "0-3/0 * * * *", "-0 * * * *", "*/+1 * * * *"})
    Q4T_CHECK(!AgentCron::Parse(invalid));
  return true;
}
}  // namespace
}  // namespace q4t::server
