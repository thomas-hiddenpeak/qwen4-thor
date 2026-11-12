#include "vector_stores_internal.h"

#include <algorithm>

#include "q4t/server/input_contract.h"

namespace q4t::server::vector_store_detail {
ObjectReply SelectFilePage(std::vector<Json> rows, const Json& page,
                           std::vector<Json>* selected, bool* more) {
  selected->clear();
  *more = false;
  std::string param;
  auto valid = InputFields(
      page, {"after", "before", "limit", "order", "filter"}, &param, "query");
  if (!valid) return Invalid(valid.message(), param);
  const auto limit = page.GetInt("limit", 20);
  const auto after = page.GetString("after"), before = page.GetString("before");
  const auto order = page.GetString("order", "desc");
  const auto filter = page.GetString("filter");
  for (const auto* key : {"after", "before", "order", "filter"})
    if (page.Has(key) && !page.Find(key)->IsString())
      return Invalid("Expected a string", key);
  if ((page.Has("limit") && !IsInputInteger(*page.Find("limit"))) ||
      limit < 1 || limit > 100 || (!after.empty() && !before.empty()) ||
      (order != "asc" && order != "desc") ||
      (!filter.empty() && filter != "in_progress" && filter != "completed" &&
       filter != "failed" && filter != "cancelled"))
    return Invalid("Invalid file pagination", "query");
  std::sort(rows.begin(), rows.end(), [&](const auto& a, const auto& b) {
    const auto x = std::pair(a.GetInt("created_at"), a.GetString("file_id"));
    const auto y = std::pair(b.GetInt("created_at"), b.GetString("file_id"));
    return order == "desc" ? x > y : x < y;
  });
  size_t begin = 0, end = rows.size();
  if (!after.empty() || !before.empty()) {
    const auto cursor = after.empty() ? before : after;
    const auto found = std::find_if(
        rows.begin(), rows.end(),
        [&](const auto& r) { return r.GetString("file_id") == cursor; });
    if (found == rows.end())
      return Invalid("Cursor is not in this list", "query");
    const auto position = static_cast<size_t>(found - rows.begin());
    if (after.empty())
      end = position;
    else
      begin = position + 1;
  }
  for (size_t i = begin; i < end; ++i) {
    if (rows[i].GetBool("detached") ||
        (!filter.empty() && rows[i].GetString("status") != filter))
      continue;
    if (selected->size() == static_cast<size_t>(limit)) {
      *more = true;
      break;
    }
    selected->push_back(std::move(rows[i]));
  }
  return {};
}
ObjectReply FilePage(Json items, bool more) {
  auto first = items.array.empty() ? Json() : *items.array.front().Find("id");
  auto last = items.array.empty() ? Json() : *items.array.back().Find("id");
  return Result(Object({{"object", String("list")},
                        {"data", std::move(items)},
                        {"first_id", std::move(first)},
                        {"last_id", std::move(last)},
                        {"has_more", Boolean(more)}}));
}
}  // namespace q4t::server::vector_store_detail
