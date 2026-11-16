#include "q4t/server/responses_mcp_media.h"

#include <algorithm>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Capacity() {
  return Error(413, "file_input_capacity_exceeded",
               "Media exceeds the shared request preparation budget");
}
ObjectReply Invalid(std::string message) {
  return Error(503, "mcp_media_preparation_invalid", std::move(message));
}
}  // namespace

void AddResponsesMediaBudget(const ResponsesMediaBudget& source,
                             ResponsesMediaBudget* destination) {
  const auto& s = source.usage;
  auto& d = destination->usage;
  d.source_bytes += s.source_bytes;
  d.text_bytes += s.text_bytes;
  d.image_bytes += s.image_bytes;
  d.pixels += s.pixels;
  d.augmentation_bytes += s.augmentation_bytes;
  d.video_scanned_frames += s.video_scanned_frames;
  d.video_decoded_pixels += s.video_decoded_pixels;
  d.video_metadata_bytes += s.video_metadata_bytes;
  d.videos += s.videos;
  d.video_frames += s.video_frames;
  d.model_input_tokens += s.model_input_tokens;
  d.model_output_tokens += s.model_output_tokens;
  d.model_steps += s.model_steps;
  d.files += s.files;
  d.images += s.images;
  d.pages += s.pages;
  d.sheets += s.sheets;
  d.cells += s.cells;
  d.embedded += s.embedded;
  destination->retained += source.retained;
  destination->rendered += source.rendered;
  destination->pending_output += source.pending_output;
}

FileExpansionOptions RemainingResponsesMediaOptions(
    const FileExpansionOptions& limits, const ResponsesMediaBudget& used) {
  auto result = limits;
  // The public policy has positive lower bounds. A harmless floor permits a
  // text-only source after the pixel budget is spent; the shared post-charge
  // below still rejects any actual overrun before publication/model use.
  const auto remaining = [](uint64_t limit, uint64_t amount,
                            uint64_t floor = 1) {
    return std::max(floor, limit - std::min(limit, amount));
  };
  const auto& u = used.usage;
  result.max_source_bytes = remaining(limits.max_source_bytes, u.source_bytes);
  result.max_files = remaining(limits.max_files, u.files);
  result.max_retained_bytes =
      remaining(limits.max_retained_bytes, used.retained);
  result.max_rendered_bytes =
      remaining(limits.max_rendered_bytes, used.rendered);
  auto& doc = result.extraction.document;
  doc.max_text_bytes =
      remaining(doc.max_text_bytes, u.text_bytes + u.augmentation_bytes);
  doc.max_sheets = remaining(doc.max_sheets, u.sheets);
  doc.max_cells = remaining(doc.max_cells, u.cells);
  doc.max_embedded_documents =
      remaining(doc.max_embedded_documents, u.embedded);
  auto& pdf = result.extraction.pdf;
  pdf.max_pages = remaining(pdf.max_pages, u.pages);
  pdf.max_image_bytes = remaining(pdf.max_image_bytes, u.image_bytes);
  pdf.max_pixels = remaining(pdf.max_pixels, u.pixels);
  auto video = result.extraction.remaining_video_work.value_or(
      FileVideoWorkBudget{result.extraction.video.max_scanned_frames,
                          result.extraction.video.max_decoded_pixels});
  video.scanned_frames -=
      std::min(video.scanned_frames, u.video_scanned_frames);
  video.decoded_pixels -=
      std::min(video.decoded_pixels, u.video_decoded_pixels);
  result.extraction.remaining_video_work = video;
  result.total_summary_output_tokens =
      remaining(limits.total_summary_output_tokens,
                u.model_output_tokens + used.pending_output, 64);
  result.summary_output_tokens = std::min(result.summary_output_tokens,
                                          result.total_summary_output_tokens);
  return result;
}

ObjectReply CheckResponsesMediaBudget(const FileExpansionOptions& limits,
                                      const ResponsesMediaBudget& used) {
  const auto& u = used.usage;
  const auto& doc = limits.extraction.document;
  const auto& pdf = limits.extraction.pdf;
  if (u.source_bytes > limits.max_source_bytes || u.files > limits.max_files ||
      u.images > limits.max_files ||
      u.text_bytes + u.augmentation_bytes > doc.max_text_bytes ||
      u.sheets > doc.max_sheets || u.cells > doc.max_cells ||
      u.embedded > doc.max_embedded_documents || u.pages > pdf.max_pages ||
      u.image_bytes > pdf.max_image_bytes || u.pixels > pdf.max_pixels ||
      u.videos > limits.max_files ||
      u.video_frames >
          limits.max_files * limits.extraction.video.sample_frames ||
      u.video_scanned_frames > limits.extraction.video.max_scanned_frames ||
      u.video_decoded_pixels > limits.extraction.video.max_decoded_pixels ||
      used.retained > limits.max_retained_bytes ||
      used.rendered > limits.max_rendered_bytes ||
      u.model_output_tokens + used.pending_output >
          static_cast<uint64_t>(limits.total_summary_output_tokens))
    return Capacity();
  return {};
}

ResponsesMediaBudget ResponsesMcpMedia::Budget() const {
  ResponsesMediaBudget result;
  for (const auto& [_, entry] : entries_) {
    ResponsesMediaBudget next;
    next.retained =
        OutputJson(entry.source).size() + OutputJson(entry.rendered).size();
    next.rendered = OutputJson(entry.rendered).size();
    if (entry.files) {
      next.usage = entry.files->Usage();
      next.retained += entry.files->RetainedBytes();
      next.pending_output = entry.files->PendingOutputBudget();
    }
    AddResponsesMediaBudget(next, &result);
  }
  return result;
}

ObjectReply ResponsesMcpMedia::Prepare(
    ResponsesMcpState& state, const Json& history,
    const FileExpansionOptions& limits, const ResponsesMediaBudget& other,
    const StateAccess& access, bool require_frozen, const Resolve& resolve) {
  if (!history.IsArray() || history.array.size() > 4096)
    return Invalid("Invalid MCP media history");
  limits_ = limits;
  other_ = other;
  auto combined = other;
  AddResponsesMediaBudget(Budget(), &combined);
  auto checked = CheckResponsesMediaBudget(limits, combined);
  if (!checked.ok()) return checked;
  for (const auto& item : history.array) {
    if (item.GetString("type") != "mcp_call" ||
        (item.GetString("status") != "completed" &&
         item.GetString("status") != "failed"))
      continue;
    const auto id = item.GetString("id");
    auto authenticated = state.Authenticate(item, access);
    if (!authenticated.ok()) return authenticated;
    if (entries_.contains(id)) continue;
    if (entries_.size() >= 4096) return Capacity();
    auto projection = state.ModelContent(id, access);
    if (!projection.ok()) return projection;
    const auto unresolved = *projection.value.GetArray("unresolved_resources");
    for (const auto& uri : unresolved.array) {
      if (require_frozen || !resolve)
        return Error(409, "mcp_resource_preparation_required",
                     "MCP resource needs its original preparation owner");
      auto result = resolve(id, uri.str);
      if (!result.ok() || result.commit_unknown) return result;
    }
    if (!unresolved.array.empty()) {
      projection = state.ModelContent(id, access);
      if (!projection.ok()) return projection;
      if (!projection.value.GetArray("unresolved_resources")->array.empty())
        return Invalid("Resource preparation did not reach a terminal state");
    }
    Entry entry;
    entry.revision = projection.value.GetInt("revision");
    entry.source = Array({Object(
        {{"id", String(id)}, {"content", *projection.value.Find("content")}})});
    for (const auto& part : projection.value.GetArray("content")->array)
      if (part.GetString("type") == "input_audio")
        return Error(503, "mcp_media_backend_unavailable",
                     "Typed MCP audio requires the audio preparation backend");
    auto used = other;
    AddResponsesMediaBudget(Budget(), &used);
    used.retained += OutputJson(entry.source).size();
    auto result = CheckResponsesMediaBudget(limits, used);
    if (!result.ok()) return result;
    if (HasFileInputs(entry.source)) {
      entry.files = std::make_unique<FileExpansion>(
          &objects_, RemainingResponsesMediaOptions(limits, used));
      result = entry.files->Prepare(
          entry.source, FileInputProfile::kResponsesStored,
          *projection.value.Find("input_snapshots"),
          {access.scope, access.deadline, access.cancelled},
          require_frozen || projection.value.GetBool("complete"));
      if (!result.ok() || result.commit_unknown) return result;
      entry.dirty = !projection.value.GetBool("complete");
      ResponsesMediaBudget prepared{entry.files->Usage(),
                                    entry.files->RetainedBytes(), 0,
                                    entry.files->PendingOutputBudget()};
      AddResponsesMediaBudget(prepared, &used);
      result = CheckResponsesMediaBudget(limits, used);
      if (!result.ok()) return result;
    }
    if (!entry.files || !entry.files->PendingSheets()) {
      entry.rendered = entry.source;
      if (entry.files) {
        result = entry.files->Render(entry.source, &entry.rendered);
        if (!result.ok()) return result;
      }
      const auto bytes = OutputJson(entry.rendered).size();
      used.retained += bytes;
      used.rendered += bytes;
      result = CheckResponsesMediaBudget(limits, used);
      if (!result.ok()) return result;
    }
    entries_.emplace(id, std::move(entry));
  }
  return {};
}

ObjectReply ResponsesMcpMedia::Persist(ResponsesMcpState& state,
                                       const StateAccess& access) {
  for (auto& [id, entry] : entries_) {
    if (!entry.files || !entry.dirty) continue;
    auto result =
        entry.files->Persist({access.scope, access.deadline, access.cancelled});
    if (result.ok() && !result.commit_unknown)
      result =
          state.BindModelSnapshots(id, entry.revision, entry.files->Bindings(),
                                   !entry.files->PendingSheets(), access);
    if (!result.ok() || result.commit_unknown) {
      if (result.commit_unknown) entry.files->RetainUncertainHolds();
      return result;
    }
    entry.revision = result.value.GetInt("revision", entry.revision);
    entry.dirty = false;
    // The MCP record owns the dependencies. Failed auxiliary hold release is
    // bounded by expiry and cannot turn a committed result into an error.
    entry.files->Release(
        {access.scope,
         std::chrono::steady_clock::now() + std::chrono::milliseconds(250),
         {}});
  }
  return {};
}

ObjectReply ResponsesMcpMedia::Render(Json* content) const {
  *content = Object();
  if (PendingSheets()) return Invalid("MCP sheet model preparation is pending");
  for (const auto& [id, entry] : entries_) {
    if (!entry.rendered.IsArray() || entry.rendered.array.size() != 1)
      return Invalid("MCP media was not rendered");
    Set(content, id, *entry.rendered.array.front().Find("content"));
  }
  return {};
}

ObjectReply ResponsesMcpMedia::PrepareSheets(
    const std::function<ObjectReply(FileExpansion&)>& prepare) {
  if (!issued_.empty()) return Invalid("A MCP sheet is already issued");
  for (auto& [_, entry] : entries_) {
    if (!entry.files || !entry.files->PendingSheets()) continue;
    if (!prepare)
      return Error(503, "file_preparation_unavailable",
                   "MCP input requires native sheet preparation");
    auto result = prepare(*entry.files);
    if (!result.ok()) return result;
    if (entry.files->PendingSheets())
      return Error(503, "file_preparation_incomplete",
                   "MCP sheet preparation did not complete");
    entry.dirty = true;
    result = entry.files->Render(entry.source, &entry.rendered);
    if (!result.ok()) return result;
    auto combined = other_;
    AddResponsesMediaBudget(Budget(), &combined);
    result = CheckResponsesMediaBudget(limits_, combined);
    if (!result.ok()) return result;
  }
  return {};
}

bool ResponsesMcpMedia::PendingSheets() const {
  return std::any_of(entries_.begin(), entries_.end(), [](const auto& pair) {
    return pair.second.files && pair.second.files->PendingSheets();
  });
}
uint64_t ResponsesMcpMedia::PendingOutputBudget() const {
  return Budget().pending_output;
}
ObjectReply ResponsesMcpMedia::NextSheet(const ChatPlanConfig& config,
                                         FileSheetStep* step) {
  if (!issued_.empty()) return Invalid("A MCP sheet is already issued");
  for (auto& [id, entry] : entries_) {
    if (!entry.files || !entry.files->PendingSheets()) continue;
    auto result = entry.files->NextSheet(config, step);
    if (result.ok()) issued_ = id;
    return result;
  }
  return Invalid("No MCP sheet is pending");
}
ObjectReply ResponsesMcpMedia::CompleteSheet(const FileSheetStep& step,
                                             std::string_view output,
                                             std::string_view finish_reason,
                                             uint64_t input_tokens,
                                             uint64_t output_tokens) {
  const auto found = entries_.find(issued_);
  if (found == entries_.end() || !found->second.files)
    return Invalid("No MCP sheet owner");
  auto& entry = found->second;
  auto result = entry.files->CompleteSheet(step, output, finish_reason,
                                           input_tokens, output_tokens);
  if (!result.ok()) return result;
  issued_.clear();
  entry.dirty = true;
  if (!entry.files->PendingSheets()) {
    result = entry.files->Render(entry.source, &entry.rendered);
    if (!result.ok()) return result;
  }
  auto combined = other_;
  AddResponsesMediaBudget(Budget(), &combined);
  return CheckResponsesMediaBudget(limits_, combined);
}
void ResponsesMcpMedia::RetainUncertainHolds() noexcept {
  for (auto& [_, entry] : entries_)
    if (entry.files) entry.files->RetainUncertainHolds();
}
}  // namespace q4t::server
