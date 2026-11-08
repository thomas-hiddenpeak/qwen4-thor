// Qwen's generated text -> OpenAI message/delta fields. No model/GPU access.
#pragma once

#include <string>
#include <optional>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/custom_tools.h"
#include "q4t/server/output_constraints.h"
#include "q4t/server/reasoning_options.h"
#include "q4t/status.h"

namespace q4t::server {

struct ResponseToolCall {
  size_t index = 0;
  std::string id;
  std::string name;
  std::string arguments;  // A JSON object, transported as a JSON string.
  bool custom = false;  // arguments instead holds the decoded custom input.
};

struct ResponseDelta {
  enum class Kind { kContent, kReasoning, kTool };
  Kind kind = Kind::kContent;
  std::string text;
  ResponseToolCall call;
  ReasoningField reasoning_field = ReasoningField::kReasoningContent;
  // Offsets in the normalized generated text, before role/tool extraction.
  // They are internal metadata, never serialized into the OpenAI response.
  size_t source_begin = 0;
  size_t source_end = 0;
  // Only the first fragment carries the call ID/type/name. Later fragments
  // append arguments for the same index; none authorizes tool execution.
  bool tool_start = true;
  bool legacy_function = false;
  std::string ToJson() const;
};

class ChatResponseParser {
 public:
  static constexpr size_t kMaxToolBytes = 1024 * 1024;
  static constexpr size_t kMaxCalls = 128;

  // request must outlive this parser; collect=false bounds streaming memory
  // to one tool block plus delimiter lookahead. Input must be valid UTF-8.
  Status Init(const io::Json& request, std::string_view prompt,
              const std::string& request_id, bool collect,
              bool legacy_function = false);
  Status Append(std::string_view text, std::vector<ResponseDelta>* deltas);
  // Intentional text stopping may end reasoning, but never a partial tool.
  Status Finish(bool length_limited, std::vector<ResponseDelta>* deltas,
                bool text_stopped = false);
  std::string MessageJson() const;
  bool HasToolCalls() const { return tool_count_ != 0; }
  const char* ToolFinishReason() const {
    return legacy_function_ ? "function_call" : "tool_calls";
  }
  bool Thinking() const { return region_ == Region::kReasoning; }
  // Earlier bytes cannot produce another delta. Deferred constrained output
  // remains live until Finish validates it and publishes all deltas.
  size_t ConsumedBytes() const {
    return constraints_.active && !finished_ ? 0 : pending_offset_;
  }

 private:
  enum class Region { kContent, kReasoning, kTool };
  Status Fail(const std::string& message);
  Status ParseTool(std::string_view block, ResponseToolCall* call) const;
  Status StreamTool(size_t available, bool complete,
                    std::vector<ResponseDelta>* deltas);
  void ToolFragment(std::string text, bool start,
                    std::vector<ResponseDelta>* deltas);
  void Emit(Region region, std::string_view text,
            std::vector<ResponseDelta>* deltas);

  bool parsed_ = false;
  bool legacy_function_ = false;
  std::optional<io::Json> normalized_request_;
  bool collect_ = false;
  bool initialized_ = false;
  bool finished_ = false;
  Region region_ = Region::kContent;
  Status status_;
  std::string id_prefix_;
  std::string pending_;
  size_t pending_offset_ = 0;
  size_t tool_scan_ = 0;
  size_t tool_count_ = 0;
  ResponseToolCall partial_call_;
  size_t tool_parse_offset_ = 0;
  size_t tool_value_begin_ = 0;
  size_t tool_value_sent_ = 0;
  size_t tool_value_scan_ = 0;
  bool tool_header_ = false;
  CustomInputStream custom_input_;
  bool tool_parameter_ = false;
  bool tool_string_ = false;
  bool tool_string_started_ = false;
  bool tool_function_closed_ = false;
  std::string tool_parameter_name_;
  std::unordered_set<std::string> tool_parameters_;
  std::unordered_map<std::string, const io::Json*> tools_;
  std::unordered_set<std::string> custom_tools_;
  std::string content_;
  std::string reasoning_;
  std::vector<ResponseToolCall> calls_;
  const io::Json* request_ = nullptr;
  OutputConstraints constraints_;
  ReasoningOptions reasoning_options_;
  size_t constrained_bytes_ = 0;
  std::vector<ResponseDelta> deferred_;
};

}  // namespace q4t::server
