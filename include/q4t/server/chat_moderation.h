#pragma once

#include "q4t/server/moderation.h"

namespace q4t::server {
// One input review and one aggregate review of all generated choices. The
// caller owns model execution, cancellation, quota and durable publication.
class ChatModeration {
 public:
  static constexpr size_t kWireLimit = 32 * 1024 * 1024;
  explicit ChatModeration(ModerationOptions options);
  bool Enabled() const { return options_.enabled; }
  bool Holding() const {
    return Enabled() && options_.output == ModerationMode::kBlock &&
           !output_done_;
  }
  Status Record(bool input, const io::Json& report, ModerationUsage usage);
  ModerationDecision Decision(bool input) const;
  bool Blocked() const;
  Status Capture(std::string_view wire);
  Status Release(std::string* wire);
  void Discard();
  Status Project(std::string_view original, std::string* output) const;
  std::string StreamMetadata(std::string_view id, std::string_view model,
                             int64_t created, bool include_usage) const;
  static Status CollectOutput(std::string_view completion,
                              ModerationContent* content, ChatPlanError* error);
  static Status EmptyCompletion(int choices, std::string_view id,
                                std::string_view model, int64_t created,
                                std::string* output);

 private:
  io::Json Reports() const;
  io::Json Usage() const;
  bool Ready() const;
  ModerationOptions options_;
  io::Json input_, output_;
  ModerationUsage usage_;
  bool input_done_ = false, output_done_ = false;
  bool failed_ = false, released_ = false;
  std::string wire_;
};
}  // namespace q4t::server
