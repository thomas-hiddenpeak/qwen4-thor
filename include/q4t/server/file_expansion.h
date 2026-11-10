// One request-owned file preparation path for generation, counting and replay.
#pragma once

#include "q4t/server/chat_plan.h"
#include "q4t/server/file_snapshot.h"
#include "q4t/server/file_image.h"

namespace q4t::server {
struct FileExpansionOptions {
  FileFetchOptions fetch;
  FileExtractOptions extraction;
  InputSnapshotLimits storage;
  FileImageLimits image;
  uint64_t max_source_bytes = kFileInputBytes;
  size_t max_files = 32;
  uint64_t max_rendered_bytes = 128ULL << 20;
  int hold_seconds = 1800;
  int summary_output_tokens = 2048;
};
struct FileExpansionUsage {
  uint64_t source_bytes = 0, text_bytes = 0, image_bytes = 0, pixels = 0;
  uint64_t augmentation_bytes = 0;
  size_t files = 0, images = 0, pages = 0, sheets = 0, cells = 0, embedded = 0;
};
struct FileSheetStep {
  size_t file_index = 0, sheet_index = 0;
  io::Json request;
  ChatRequestPlan chat;
  std::string prompt_sha256;
};
// Items need stable private IDs (ResponsesState supplies these; Chat adapters
// use private IDs and remove them before normal planning). Input items remain
// unchanged. Only content/output/results trees are expanded, never metadata
// or arbitrary tool JSON. Whole-envelope validation precedes this component.
class FileExpansion {
 public:
  FileExpansion(ObjectStore* objects, FileExpansionOptions options);
  ~FileExpansion();
  FileExpansion(const FileExpansion&) = delete;
  FileExpansion& operator=(const FileExpansion&) = delete;
  ObjectReply Prepare(const io::Json& items, FileInputProfile profile,
                      const io::Json& bindings, const BinaryAccess& access,
                      bool require_frozen = false, GrammarControl control = {});
  // Queued work can persist raw extraction before it owns a model slot.
  // After actual augmentation, Persist publishes new immutable versions;
  // caller must checkpoint Bindings() before releasing preparation holds.
  ObjectReply Persist(const BinaryAccess& access);
  ObjectReply Release(const BinaryAccess& access);
  bool PendingSheets() const;
  ObjectReply NextSheet(const ChatPlanConfig& config, FileSheetStep* step);
  // Accept only a completed real model step, with actual token accounting.
  ObjectReply CompleteSheet(const FileSheetStep& step, std::string_view output,
                            std::string_view finish_reason,
                            uint64_t input_tokens, uint64_t output_tokens);
  ObjectReply Render(const io::Json& original, io::Json* expanded) const;
  const io::Json& Bindings() const { return bindings_; }
  const FileExpansionUsage& Usage() const { return usage_; }
  bool CommitUnknown() const { return commit_unknown_; }

 private:
  struct Entry {
    std::string item_id, path, identity;
    io::Json cache_breakpoint;
    FileInputSnapshot snapshot;
    std::optional<FileImageInput> image;
    InputSnapshotRef reference;
    bool dirty = true;
  };
  ObjectStore* objects_;
  FileExpansionOptions options_;
  FileInputProfile profile_ = FileInputProfile::kResponses;
  std::string scope_;
  io::Json bindings_;
  std::vector<Entry> entries_;
  std::vector<InputSnapshotHold> holds_;
  FileExpansionUsage usage_;
  ObjectReply unknown_result_;
  std::string issued_step_sha256_;
  size_t issued_file_ = 0, issued_sheet_ = 0;
  bool prepared_ = false, commit_unknown_ = false;
};
}  // namespace q4t::server
