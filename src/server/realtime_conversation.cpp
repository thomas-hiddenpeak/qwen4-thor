#include "q4t/server/realtime_conversation.h"

#include <algorithm>
#include <array>
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
Status InputText(const Json& item, Json* copy, bool idle_timeout = false) {
  const auto* content = item.GetArray("content");
  if (!content) return Invalid("native input has no content");
  auto parts = *content;
  for (auto& part : parts.array) {
    if (part.GetString("type") != "input_audio") continue;
    const auto* transcript = part.Find("transcript");
    if (!transcript || !transcript->IsString())
      return Invalid("native input has no completed ASR transcript");
    // Execution snapshot only. Client transcripts never gain this authority.
    const auto text = idle_timeout && transcript->str.empty()
                          ? String(
                                "[No speech was detected during the idle "
                                "interval. Invite the user to continue.]")
                          : *transcript;
    part = Object({{"type", String("input_text")}, {"text", text}});
  }
  Set(copy, "content", std::move(parts));
  return {};
}

}  // namespace

Status MeasureRealtimeStorage(const Json& value, size_t maximum,
                              size_t* bytes) {
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
    if (entry.item.GetString("id") == id &&
        (!entry.native_input || entry.input_committed)) {
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
  if (entries_[index].native_input && !entries_[index].input_committed)
    return Invalid("input has not been committed");
  auto copy = entries_[index].item;
  if (entries_[index].native_input && !entries_[index].transcript_visible)
    for (auto& part : Field(copy, "content").array)
      if (part.GetString("type") == "input_audio")
        std::erase_if(part.object, [](const auto& field) {
          return field.first == "transcript";
        });
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
Status RealtimeConversation::BeginAudioInput(
    std::string_view id, std::optional<std::string_view> previous,
    bool transcript_visible, size_t reserved_bytes,
    RealtimeItemTicket* output) {
  return InsertAudioInput(id, previous, true, transcript_visible,
                          reserved_bytes, output);
}
Status RealtimeConversation::ReserveAudioInput(std::string_view id,
                                               bool transcript_visible,
                                               size_t reserved_bytes,
                                               RealtimeItemTicket* output) {
  return InsertAudioInput(id, {}, false, transcript_visible, reserved_bytes,
                          output);
}
Status RealtimeConversation::ReserveAudioInput(const Json& input,
                                               std::string_view generated_id,
                                               bool transcript_visible,
                                               size_t reserved_bytes,
                                               RealtimeItemTicket* output) {
  if (input.GetString("type") != "message" ||
      input.GetString("role") != "user" || !reserved_bytes)
    return Invalid("native audio input requires a reserved user message");
  auto status = Shape(input, maximum_bytes_);
  if (!status) return status;
  auto item = input;
  auto* content = item.Has("content") ? &Field(item, "content") : nullptr;
  if (!content || !content->IsArray() || content->array.size() > 128)
    return Invalid("native audio content exceeds its part limit");
  std::vector<size_t> parts;
  for (size_t index = 0; index < content->array.size(); ++index) {
    auto& part = content->array[index];
    if (part.GetString("type") != "input_audio") continue;
    parts.push_back(index);
    std::erase_if(part.object, [](const auto& field) {
      return field.first == "audio" || field.first == "transcript";
    });
  }
  if (parts.empty()) return Invalid("native audio part is missing");
  Json canonical;
  status = Canonical(item, generated_id, true, reserved_bytes, &canonical);
  if (!status) return status;
  size_t bytes = 0;
  status = Measure(canonical, reserved_bytes, &bytes);
  if (!status || parts.capacity() * sizeof(size_t) > reserved_bytes - bytes)
    return Invalid("native audio part reservation is insufficient");
  status = Insert(canonical, generated_id, {}, false, reserved_bytes, output);
  if (!status) return status;
  auto& entry = entries_[Position(*output)];
  entry.native_input = true;
  entry.transcript_visible = transcript_visible;
  entry.server_output = false;
  entry.pending_input_parts = std::move(parts);
  Field(entry.item, "status").str = "completed";
  return {};
}
Status RealtimeConversation::InsertAudioInput(
    std::string_view id, std::optional<std::string_view> previous,
    bool committed, bool transcript_visible, size_t reserved_bytes,
    RealtimeItemTicket* output) {
  if (!reserved_bytes) return Invalid("input needs a reservation");
  const auto item =
      Object({{"type", String("message")},
              {"role", String("user")},
              {"content", Array({Object({{"type", String("input_audio")}})})}});
  std::vector<size_t> parts{0};
  Json canonical;
  auto status = Canonical(item, id, true, reserved_bytes, &canonical);
  size_t bytes = 0;
  if (status) status = Measure(canonical, reserved_bytes, &bytes);
  if (!status || parts.capacity() * sizeof(size_t) > reserved_bytes - bytes)
    return Invalid("native audio part reservation is insufficient");
  status = Insert(canonical, id, previous, committed, reserved_bytes, output);
  if (!status) return status;
  auto& entry = entries_[Position(*output)];
  entry.native_input = true;
  entry.input_committed = committed;
  entry.transcript_visible = transcript_visible;
  entry.server_output = false;
  entry.pending_input_parts = std::move(parts);
  // Public status on eventual commit is independent of the pending ASR.
  Field(entry.item, "status").str = "completed";
  return {};
}
Status RealtimeConversation::CommitAudioInput(
    const RealtimeItemTicket& ticket, std::optional<std::string_view> previous,
    bool in_conversation) {
  const auto index = Position(ticket);
  if (index == entries_.size() || !entries_[index].native_input ||
      entries_[index].input_committed || !entries_[index].pending ||
      revision_ == UINT64_MAX || (!in_conversation && previous))
    return Invalid("native input reservation is stale or already committed");
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
  // All validation precedes non-allocating moves inside the reserved vector.
  auto& entry = entries_[index];
  entry.input_committed = true;
  entry.in_conversation = in_conversation;
  if (position < index)
    std::rotate(entries_.begin() + position, entries_.begin() + index,
                entries_.begin() + index + 1);
  else if (position > index + 1)
    std::rotate(entries_.begin() + index, entries_.begin() + index + 1,
                entries_.begin() + position);
  ++revision_;
  return {};
}
Status RealtimeConversation::CompleteAudioInput(
    const RealtimeItemTicket& ticket, std::string_view transcript,
    bool idle_timeout) {
  const auto position = Position(ticket);
  if (position == entries_.size() || !entries_[position].native_input ||
      entries_[position].item.GetArray("content")->array.size() != 1)
    return Invalid("single audio completion requires one content part");
  const auto status = FinishAudioPart(ticket, 0, transcript);
  if (status) entries_[Position(ticket)].idle_timeout = idle_timeout;
  return status;
}
Status RealtimeConversation::FailAudioInput(const RealtimeItemTicket& ticket) {
  return FinishAudioInput(ticket, std::nullopt);
}
Status RealtimeConversation::CompleteAudioPart(const RealtimeItemTicket& ticket,
                                               size_t index,
                                               std::string_view transcript) {
  return FinishAudioPart(ticket, index, transcript);
}
Status RealtimeConversation::FailAudioPart(const RealtimeItemTicket& ticket,
                                           size_t index) {
  return FinishAudioPart(ticket, index, std::nullopt);
}
Status RealtimeConversation::FinishAudioPart(
    const RealtimeItemTicket& ticket, size_t index,
    std::optional<std::string_view> transcript) {
  const auto position = Position(ticket);
  if (position == entries_.size()) return Invalid("native input is stale");
  auto& entry = entries_[position];
  const auto pending = std::ranges::find(entry.pending_input_parts, index);
  if (!entry.native_input || !entry.input_committed || !entry.pending ||
      pending == entry.pending_input_parts.end() || revision_ == UINT64_MAX ||
      (transcript && transcript->size() > entry.charge))
    return Invalid("native audio part is stale or exceeds its reservation");
  auto item = entry.item;
  if (transcript)
    Set(&Field(item, "content").array[index], "transcript",
        String(std::string(*transcript)));
  auto status = Shape(item, entry.charge);
  size_t bytes = 0;
  if (status) status = Measure(item, entry.charge, &bytes);
  if (!status) return status;
  const bool terminal = entry.pending_input_parts.size() == 1;
  if (!terminal && entry.pending_input_parts.capacity() * sizeof(size_t) >
                       entry.charge - bytes)
    return Invalid("native input exceeds its retained reservation");
  entry.item = std::move(item);
  entry.pending_input_parts.erase(pending);
  entry.input_failed |= !transcript;
  if (terminal) {
    charged_ -= entry.charge - bytes;
    entry.charge = bytes;
    entry.pending = false;
    std::vector<size_t>{}.swap(entry.pending_input_parts);
  }
  ++revision_;
  return {};
}
Status RealtimeConversation::FinishAudioInput(
    const RealtimeItemTicket& ticket,
    std::optional<std::string_view> transcript) {
  const auto index = Position(ticket);
  if (index == entries_.size() || !entries_[index].native_input ||
      !entries_[index].pending)
    return Invalid("native input ticket is stale or already terminal");
  if (!entries_[index].input_committed)
    return transcript ? Invalid("native input has not been committed")
                      : Erase(ticket);
  if (transcript && transcript->size() > entries_[index].charge)
    return Invalid("input transcript exceeds its reservation");
  auto item = entries_[index].item;
  if (transcript)
    Set(&Field(item, "content").array[0], "transcript",
        String(std::string(*transcript)));
  auto status = UpdateEntry(ticket, item, true, false);
  if (status) {
    entries_[index].input_failed = !transcript;
    std::vector<size_t>{}.swap(entries_[index].pending_input_parts);
  }
  return status;
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
  entry.server_output = entry.pending;
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
                                    const Json& item, bool terminal,
                                    bool grow) {
  const auto index = Position(ticket);
  if (index != entries_.size() && entries_[index].native_input)
    return Invalid("native input requires its ASR completion owner");
  return UpdateEntry(ticket, item, terminal, grow);
}
Status RealtimeConversation::UpdateEntry(const RealtimeItemTicket& ticket,
                                         const Json& item, bool terminal,
                                         bool grow) {
  const auto index = Position(ticket);
  if (index == entries_.size() || !entries_[index].pending ||
      revision_ == UINT64_MAX)
    return Invalid("output ticket is stale or already terminal");
  auto& entry = entries_[index];
  const auto available = entry.charge + (grow ? maximum_bytes_ - charged_ : 0);
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
Status RealtimeConversation::TruncateAudioTranscript(
    const RealtimeItemTicket& ticket) {
  const auto index = Position(ticket);
  if (index == entries_.size() || !entries_[index].server_output ||
      revision_ == UINT64_MAX)
    return Invalid("audio output ticket is stale");
  auto& item = entries_[index].item;
  const auto* content = item.GetArray("content");
  if (item.GetString("role") != "assistant" || !content ||
      content->array.size() != 1 ||
      content->array[0].GetString("type") != "output_audio")
    return Invalid("item has no generated audio transcript");
  auto& part = Field(item, "content").array[0];
  if (!part.Find("transcript") || !part.Find("transcript")->IsString())
    return Invalid("audio transcript is unavailable");
  Field(part, "transcript").str.clear();
  ++revision_;
  return {};
}

Status RealtimeConversation::Snapshot(
    const Json* input, RealtimeConversationSnapshot* output) const {
  return SnapshotImpl(input, false, false, output);
}
Status RealtimeConversation::Capture(const Json* input,
                                     RealtimeConversationSnapshot* output,
                                     bool wait_for_output) const {
  return SnapshotImpl(input, true, wait_for_output, output);
}
Status RealtimeConversation::SnapshotImpl(
    const Json* input, bool allow_pending, bool wait_for_output,
    RealtimeConversationSnapshot* output) const {
  if (!valid_ || !output ||
      (input && (!input->IsArray() || input->array.size() > maximum_items_)))
    return Invalid("invalid snapshot input");
  RealtimeConversationSnapshot snapshot{owner_, revision_, Array()};
  snapshot.default_context = !input;
  size_t remaining = maximum_bytes_;
  const auto unexecutable = [](const Entry& entry) {
    return (entry.native_input && entry.input_failed) ||
           (entry.server_output && !entry.pending &&
            entry.item.GetString("type") == "function_call" &&
            entry.item.GetString("status") == "incomplete");
  };
  auto append = [&](const Json& item, bool pending, uint64_t generation = 0,
                    bool native_input = false,
                    bool idle_timeout = false) -> Status {
    if (pending && (!allow_pending || (!native_input && !wait_for_output)))
      return Invalid("input or output item is still pending");
    if (snapshot.items.array.size() == maximum_items_)
      return Invalid("snapshot item limit exceeded");
    auto status = Shape(item, remaining);
    if (!status) return status;
    auto copy = item;
    if (native_input && !pending &&
        !(status = InputText(item, &copy, idle_timeout)))
      return status;
    size_t bytes = 0;
    status = Measure(copy, remaining, &bytes);
    if (!status) return status;
    if (pending)
      (native_input ? snapshot.pending_inputs : snapshot.pending_outputs)
          .push_back(snapshot.items.array.size());
    snapshot.items.array.push_back(std::move(copy));
    snapshot.tickets.push_back({owner_, item.GetString("id"), generation});
    remaining -= bytes;
    return {};
  };
  if (!input) {
    for (const auto& entry : entries_) {
      if (!entry.in_conversation || !entry.in_context || unexecutable(entry))
        continue;
      auto status = append(entry.item, entry.pending, entry.generation,
                           entry.native_input, entry.idle_timeout);
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
        if (unexecutable(entry))
          return Invalid("failed native input or incomplete tool context");
        status = append(entry.item, entry.pending, entry.generation,
                        entry.native_input, entry.idle_timeout);
      } else {
        status = append(item, false);
      }
      if (!status) return status;
    }
  }
  *output = std::move(snapshot);
  return {};
}

Status RealtimeConversation::Resolve(RealtimeConversationSnapshot* snapshot,
                                     bool* ready) const {
  if (ready) *ready = false;
  if (!valid_ || !snapshot || !ready || snapshot->owner != owner_ ||
      snapshot->revision > revision_ || !snapshot->items.IsArray() ||
      snapshot->items.array.size() > maximum_items_ ||
      snapshot->tickets.size() != snapshot->items.array.size() ||
      snapshot->pending_inputs.size() > snapshot->items.array.size() ||
      snapshot->pending_outputs.size() > snapshot->items.array.size())
    return Invalid("invalid pending input snapshot");
  std::array<bool, kMaximumItems> waiting{};
  std::array<bool, kMaximumItems> waiting_output{}, omit{};
  bool pending = false;
  for (const auto index : snapshot->pending_inputs) {
    if (index >= snapshot->items.array.size() || waiting[index])
      return Invalid("invalid pending input position");
    waiting[index] = true;
    const auto& ticket = snapshot->tickets[index];
    const auto position = Position(ticket);
    if (position == entries_.size() ||
        ticket.id != snapshot->items.array[index].GetString("id"))
      return Invalid("pending input generation was deleted");
    const auto& entry = entries_[position];
    if (!entry.native_input || !entry.input_committed || entry.input_failed ||
        !SameIdentity(snapshot->items.array[index], entry.item))
      return Invalid("pending input failed or lost its native owner");
    pending |= entry.pending;
  }
  for (const auto index : snapshot->pending_outputs) {
    if (index >= snapshot->items.array.size() || waiting[index] ||
        waiting_output[index])
      return Invalid("invalid pending output position");
    waiting_output[index] = true;
    const auto& ticket = snapshot->tickets[index];
    const auto position = Position(ticket);
    if (position == entries_.size())
      return Invalid("pending output generation was deleted");
    const auto& entry = entries_[position];
    if (!entry.server_output || entry.native_input ||
        !SameIdentity(snapshot->items.array[index], entry.item))
      return Invalid("pending output lost its native owner");
    if (!entry.pending && entry.item.GetString("type") == "function_call" &&
        entry.item.GetString("status") == "incomplete") {
      if (!snapshot->default_context)
        return Invalid("explicit output reference is an incomplete tool");
      omit[index] = true;
    }
    pending |= entry.pending;
  }
  if (pending) return {};
  if (!snapshot->pending_inputs.empty() || !snapshot->pending_outputs.empty()) {
    auto copy = *snapshot;
    size_t remaining = maximum_bytes_;
    for (size_t i = 0; i < copy.items.array.size(); ++i) {
      auto& item = copy.items.array[i];
      if (omit[i]) continue;
      if (waiting[i]) {
        const auto& entry = entries_[Position(copy.tickets[i])];
        auto status = InputText(entry.item, &item, entry.idle_timeout);
        if (!status) return status;
      }
      if (waiting_output[i]) item = entries_[Position(copy.tickets[i])].item;
      size_t bytes = 0;
      auto status = Measure(item, remaining, &bytes);
      if (!status) return status;
      remaining -= bytes;
    }
    for (size_t i = copy.items.array.size(); i-- > 0;) {
      if (!omit[i]) continue;
      copy.items.array.erase(copy.items.array.begin() + i);
      copy.tickets.erase(copy.tickets.begin() + i);
    }
    copy.pending_inputs.clear();
    copy.pending_outputs.clear();
    *snapshot = std::move(copy);
  }
  *ready = true;
  return {};
}

Status RealtimeConversation::TruncateDefault(
    const RealtimeConversationSnapshot& snapshot,
    std::span<const size_t> retained) {
  if (!valid_ || snapshot.owner != owner_ || !snapshot.default_context ||
      !snapshot.items.IsArray() || !snapshot.pending_inputs.empty() ||
      !snapshot.pending_outputs.empty() ||
      snapshot.items.array.size() > maximum_items_ ||
      snapshot.tickets.size() != snapshot.items.array.size() ||
      revision_ == UINT64_MAX)
    return Invalid("invalid context truncation owner");
  std::array<bool, kMaximumItems> keep{};
  for (const auto index : retained) {
    if (index >= snapshot.items.array.size() || keep[index])
      return Invalid("invalid retained context index");
    keep[index] = true;
  }
  for (size_t i = 0; i < snapshot.tickets.size(); ++i) {
    const auto& ticket = snapshot.tickets[i];
    if (ticket.owner != owner_ || !ticket.generation ||
        ticket.id != snapshot.items.array[i].GetString("id"))
      return Invalid("invalid context generation ticket");
  }
  bool changed = false;
  for (size_t i = 0; i < snapshot.tickets.size(); ++i) {
    if (keep[i]) continue;
    const auto position = Position(snapshot.tickets[i]);
    if (position == entries_.size()) continue;
    auto& entry = entries_[position];
    if (entry.in_conversation && entry.in_context) {
      entry.in_context = false;
      changed = true;
    }
  }
  if (changed) ++revision_;
  return {};
}
}  // namespace q4t::server
