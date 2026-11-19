#include "q4t/server/realtime_conversation.h"

#include <algorithm>
#include <limits>

#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_shape.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Status Invalid(std::string_view reason) {
  return Status::Fail("Realtime conversation: " + std::string(reason));
}
bool Spend(size_t amount, size_t* remaining) {
  if (amount > *remaining) return false;
  *remaining -= amount;
  return true;
}
// Charge allocated capacities, not just visible text. The extra sizeof(Json)
// per child deliberately overcounts inline nodes to keep this bound simple.
bool Charge(const Json& value, size_t* remaining, size_t* nodes,
            size_t depth = 0) {
  if (depth > 64 || !*nodes) return false;
  --*nodes;
  if (!Spend(sizeof(Json), remaining) ||
      !Spend(value.str.capacity(), remaining) ||
      !Spend(value.array.capacity() * sizeof(Json), remaining) ||
      !Spend(
          value.object.capacity() * sizeof(decltype(value.object)::value_type),
          remaining))
    return false;
  for (const auto& item : value.array)
    if (!Charge(item, remaining, nodes, depth + 1)) return false;
  for (const auto& [key, item] : value.object)
    if (!Spend(key.capacity(), remaining) ||
        !Charge(item, remaining, nodes, depth + 1))
      return false;
  return true;
}
Status Measure(const Json& value, size_t maximum, size_t* bytes) {
  size_t remaining = maximum, nodes = 65536;
  if (!Charge(value, &remaining, &nodes))
    return Invalid("item storage limit exceeded");
  *bytes = maximum - remaining;
  return {};
}
Status Shape(const Json& item, size_t maximum) {
  size_t bytes = 0;
  auto status = Measure(item, maximum, &bytes);
  if (!status) return status;
  ChatPlanError error;
  return ValidateRealtimeShape(item, RealtimeShape::kItem, &error, "item");
}
bool Ordinary(const Json& item) {
  const auto type = item.GetString("type");
  return type == "message" || type == "function_call" ||
         type == "function_call_output";
}
Status Canonical(const Json& input, std::string_view generated_id, bool pending,
                 size_t maximum, Json* output) {
  auto status = Shape(input, maximum);
  if (!status) return status;
  auto item = input;
  if (!item.Has("id")) Set(&item, "id", String(std::string(generated_id)));
  if (!ValidRequestId(item.GetString("id")) || item.GetString("id") == "root")
    return Invalid("item ID is invalid");
  if (Ordinary(item)) {
    Set(&item, "object", String("realtime.item"));
    if (pending || !item.Has("status"))
      Set(&item, "status", String(pending ? "in_progress" : "completed"));
  }
  if (item.GetString("type") == "function_call" && !item.Has("call_id")) {
    if (!ValidRequestId(generated_id)) return Invalid("call ID is invalid");
    Set(&item, "call_id", String(std::string(generated_id)));
  }
  if (item.Has("call_id") && !ValidRequestId(item.GetString("call_id")))
    return Invalid("call ID is invalid");
  status = Shape(item, maximum);
  if (status) *output = std::move(item);
  return status;
}
bool SameIdentity(const Json& left, const Json& right) {
  for (const char* key : {"id", "type", "role", "call_id", "name",
                          "server_label", "approval_request_id"}) {
    const auto* a = left.Find(key);
    const auto* b = right.Find(key);
    if ((a == nullptr) != (b == nullptr)) return false;
    if (!a || (a->IsNull() && b->IsNull())) continue;
    if (!a->IsString() || !b->IsString() || a->str != b->str) return false;
  }
  return true;
}
}  // namespace

Status MeasureRealtimeStorage(const Json& value, size_t maximum, size_t* bytes) {
  if (!bytes) return Invalid("storage result is missing");
  return Measure(value, maximum, bytes);
}

RealtimeConversation::RealtimeConversation(std::string owner,
                                           size_t maximum_items,
                                           size_t maximum_bytes)
    : owner_(std::move(owner)),
      maximum_items_(maximum_items),
      maximum_bytes_(maximum_bytes) {
  valid_ = ValidRequestId(owner_) && maximum_items_ &&
           maximum_items_ <= kMaximumItems && maximum_bytes_ &&
           maximum_bytes_ <= kMaximumBytes;
  if (valid_) entries_.reserve(maximum_items_);
}
size_t RealtimeConversation::Position(const RealtimeItemTicket& ticket) const {
  if (valid_ && ticket.owner == owner_ && ticket.generation)
    for (size_t i = 0; i < entries_.size(); ++i)
      if (entries_[i].generation == ticket.generation &&
          entries_[i].item.GetString("id") == ticket.id)
        return i;
  return entries_.size();
}
std::string RealtimeConversation::LastId() const {
  for (auto it = entries_.rbegin(); it != entries_.rend(); ++it)
    if (it->in_conversation) return it->item.GetString("id");
  return {};
}
Status RealtimeConversation::Find(std::string_view id,
                                  RealtimeItemTicket* output) const {
  if (!valid_ || !output) return Invalid("invalid history owner");
  for (const auto& entry : entries_)
    if (entry.item.GetString("id") == id) {
      RealtimeItemTicket ticket{owner_, std::string(id), entry.generation};
      *output = std::move(ticket);
      return {};
    }
  return Invalid("item not found");
}
Status RealtimeConversation::Get(const RealtimeItemTicket& ticket,
                                 Json* output) const {
  const auto index = Position(ticket);
  if (index == entries_.size() || !output) return Invalid("item not found");
  auto copy = entries_[index].item;
  *output = std::move(copy);
  return {};
}
Status RealtimeConversation::Erase(const RealtimeItemTicket& ticket) {
  const auto index = Position(ticket);
  if (index == entries_.size() || revision_ == UINT64_MAX)
    return Invalid("item not found or revision exhausted");
  charged_ -= entries_[index].charge;
  entries_.erase(entries_.begin() + index);
  ++revision_;
  return {};
}
Status RealtimeConversation::Create(const Json& item,
                                    std::string_view generated_id,
                                    std::optional<std::string_view> previous,
                                    RealtimeItemTicket* output) {
  return Insert(item, generated_id, previous, true, 0, output);
}
Status RealtimeConversation::BeginOutput(const Json& item,
                                         std::string_view generated_id,
                                         bool in_conversation,
                                         size_t reserved_bytes,
                                         RealtimeItemTicket* output) {
  if (!reserved_bytes) return Invalid("output needs a reservation");
  return Insert(item, generated_id, {}, in_conversation, reserved_bytes,
                output);
}
Status RealtimeConversation::Insert(const Json& input,
                                    std::string_view generated_id,
                                    std::optional<std::string_view> previous,
                                    bool in_conversation, size_t reserved_bytes,
                                    RealtimeItemTicket* output) {
  if (!valid_ || !output || entries_.size() == maximum_items_ ||
      generation_ == UINT64_MAX || revision_ == UINT64_MAX ||
      reserved_bytes > maximum_bytes_ - charged_)
    return Invalid("history capacity exhausted or invalid owner");
  size_t position = entries_.size();
  if (previous) {
    position = 0;
    if (*previous != "root") {
      const auto it = std::find_if(
          entries_.begin(), entries_.end(), [&](const auto& entry) {
            return entry.in_conversation &&
                   entry.item.GetString("id") == *previous;
          });
      if (it == entries_.end()) return Invalid("predecessor not found");
      position = static_cast<size_t>(it - entries_.begin()) + 1;
    }
  }
  Entry entry;
  auto status = Canonical(input, generated_id, reserved_bytes != 0,
                          maximum_bytes_, &entry.item);
  if (!status) return status;
  const auto id = entry.item.GetString("id");
  for (const auto& old : entries_)
    if (old.item.GetString("id") == id) return Invalid("duplicate item ID");
  status = Measure(entry.item,
                   reserved_bytes ? reserved_bytes : maximum_bytes_ - charged_,
                   &entry.charge);
  if (!status) return status;
  entry.pending = reserved_bytes != 0;
  if (entry.pending) entry.charge = reserved_bytes;
  entry.in_conversation = in_conversation;
  entry.generation = generation_ + 1;
  RealtimeItemTicket ticket{owner_, id, entry.generation};
  const size_t charge = entry.charge;
  entries_.insert(entries_.begin() + position, std::move(entry));
  charged_ += charge;
  ++generation_;
  ++revision_;
  *output = std::move(ticket);
  return {};
}
Status RealtimeConversation::Update(const RealtimeItemTicket& ticket,
                                    const Json& item, bool terminal, bool grow) {
  const auto index = Position(ticket);
  if (index == entries_.size() || !entries_[index].pending ||
      revision_ == UINT64_MAX)
    return Invalid("output ticket is stale or already terminal");
  auto& entry = entries_[index];
  const auto available =
      entry.charge + (grow ? maximum_bytes_ - charged_ : 0);
  auto status = Shape(item, available);
  if (!status) return status;
  if (!SameIdentity(entry.item, item))
    return Invalid("output identity changed");
  if (Ordinary(item)) {
    const auto state = item.GetString("status");
    if ((!terminal && state != "in_progress") ||
        (terminal && state != "completed" && state != "incomplete"))
      return Invalid("output status does not match terminal state");
  }
  auto copy = item;
  size_t bytes = 0;
  status = Measure(copy, available, &bytes);
  if (!status) return status;
  entry.item = std::move(copy);
  const auto charge = terminal ? bytes : std::max(entry.charge, bytes);
  charged_ = charged_ - entry.charge + charge;
  entry.charge = charge;
  if (terminal) entry.pending = false;
  ++revision_;
  return {};
}
Status RealtimeConversation::Snapshot(
    const Json* input, RealtimeConversationSnapshot* output) const {
  if (!valid_ || !output ||
      (input && (!input->IsArray() || input->array.size() > maximum_items_)))
    return Invalid("invalid snapshot input");
  RealtimeConversationSnapshot snapshot{owner_, revision_, Array()};
  size_t remaining = maximum_bytes_;
  auto append = [&](const Json& item, bool pending) -> Status {
    if (pending) return Invalid("output item is still pending");
    if (snapshot.items.array.size() == maximum_items_)
      return Invalid("snapshot item limit exceeded");
    auto status = Shape(item, remaining);
    if (!status) return status;
    auto copy = item;
    size_t bytes = 0;
    status = Measure(copy, remaining, &bytes);
    if (!status) return status;
    snapshot.items.array.push_back(std::move(copy));
    remaining -= bytes;
    return {};
  };
  if (!input) {
    for (const auto& entry : entries_) {
      if (!entry.in_conversation) continue;
      auto status = append(entry.item, entry.pending);
      if (!status) return status;
    }
  } else {
    for (const auto& item : input->array) {
      Status status;
      if (item.GetString("type") == "item_reference") {
        status = InputFields(item, {"type", "id"});
        const auto* id = item.Find("id");
        RealtimeItemTicket ticket;
        if (!status || item.object.size() != 2 || !id || !id->IsString() ||
            !Find(id->str, &ticket))
          return Invalid("item reference is not in this session");
        const auto& entry = entries_[Position(ticket)];
        status = append(entry.item, entry.pending);
      } else {
        status = append(item, false);
      }
      if (!status) return status;
    }
  }
  *output = std::move(snapshot);
  return {};
}
}  // namespace q4t::server
