#include "chatkit_widget_stream.h"

#include <algorithm>
#include <unordered_set>

#include "object_protocol.h"
#include "q4t/server/schema_graph.h"

namespace q4t::server::chatkit_detail {
namespace {
using namespace object_detail;

bool Component(const Json& node, std::unordered_set<std::string>* ids) {
  if (!node.IsObject() || node.GetString("type").empty()) return false;
  for (const auto* name : {"id", "key"}) {
    const auto* field = node.Find(name);
    if (field && !field->IsString() && !field->IsNull()) return false;
  }
  const auto id = node.GetString("id");
  if (ids && !id.empty() && !ids->insert(id).second) return false;
  const auto* children = node.Find("children");
  if (!children || children->IsNull()) return true;
  if (children->IsObject()) return Component(*children, ids);
  return children->IsArray() &&
         std::ranges::all_of(children->array, [&](const Json& child) {
           return Component(child, ids);
         });
}

bool AppendDeltas(const Json& before, const Json& after, Json* updates) {
  if (before.type != after.type) return false;
  if (after.IsNull()) return true;
  if (after.IsArray()) {
    if (before.array.size() != after.array.size()) return false;
    for (size_t i = 0; i < after.array.size(); ++i)
      if (!AppendDeltas(before.array[i], after.array[i], updates)) return false;
    return true;
  }
  if (!after.IsObject()) return false;
  const auto type = after.GetString("type");
  const auto* old_value = before.Find("value");
  const auto* value = after.Find("value");
  const auto id = after.GetString("id");
  const bool text = (type == "Text" || type == "Markdown") && !id.empty() &&
                    old_value && old_value->IsString() && value &&
                    value->IsString();
  // Compare other properties by JSON value, independent of JS key order.
  auto properties = [&](const Json& node) {
    Json result = Object();
    for (const auto& field : node.object)
      if (field.first != "children" && !(text && field.first == "value"))
        result.object.push_back(field);
    return result;
  };
  std::string left, right;
  if (!CanonicalSchemaValue(properties(before), &left) ||
      !CanonicalSchemaValue(properties(after), &right) || left != right)
    return false;
  if (text && old_value->str != value->str) {
    const auto* streaming = after.Find("streaming");
    if ((streaming && !streaming->IsBool()) ||
        !value->str.starts_with(old_value->str) || updates->array.size() >= 16)
      return false;
    updates->array.push_back(
        Object({{"type", String("widget.streaming_text.value_delta")},
                {"component_id", String(id)},
                {"delta", String(value->str.substr(old_value->str.size()))},
                {"done", Boolean(!after.GetBool("streaming"))}}));
  }
  const auto* old_children = before.Find("children");
  const auto* children = after.Find("children");
  if (!old_children || !children) return old_children == children;
  return AppendDeltas(*old_children, *children, updates);
}
}  // namespace

bool ValidWidget(const io::Json& root, bool require_unique_ids) {
  const auto type = root.GetString("type");
  std::unordered_set<std::string> ids;
  return (type == "Card" || type == "Basic" || type == "ListView") &&
         Component(root, require_unique_ids ? &ids : nullptr);
}

io::Json WidgetUpdates(const io::Json& before, const io::Json& after) {
  auto updates = object_detail::Array();
  if (AppendDeltas(before, after, &updates)) return updates;
  return object_detail::Array({object_detail::Object(
      {{"type", object_detail::String("widget.root.updated")},
       {"widget", after}})});
}
}  // namespace q4t::server::chatkit_detail
