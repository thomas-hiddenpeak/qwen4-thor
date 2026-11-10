// Input tokens retained by an enqueued request, independent of per-minute
// accounting of all actual model steps. Counting never generates a summary.
#pragma once

#include "q4t/server/batch_input.h"
#include "q4t/server/binary_store.h"
#include "q4t/server/completion_plan.h"
#include "q4t/server/media_admission.h"

namespace q4t::server {
struct BatchInputBudget {
  uint64_t tokens = 0;
  // A pending sheet summary has not been generated. Reserve its final context
  // ceiling, explicitly distinguished from an exact token count.
  bool exact = true;
};
inline constexpr uint64_t kBatchTokenLimit = (uint64_t{1} << 53) - 1;
io::Json BatchInputBudgetJson(const BatchInputBudget& budget);
bool ParseBatchInputBudget(const io::Json& value, BatchInputBudget* budget);
struct BatchTokenCodec {
  PromptEncoder encode;
  CompletionDecoder decode;
  std::function<Status(const io::Json&, std::span<const VisionItem>,
                       const ChatPlanConfig&, std::vector<uint32_t>*,
                       ChatPromptBudget*, ChatPlanError*)>
      expand_media;
};
using BatchInputMeter = std::function<ObjectReply(
    std::string_view, const BatchInputRow&, const io::Json&,
    const ChatPlanConfig&, const BinaryAccess&, BatchInputBudget*)>;

// Reads only the prepared immutable inputs. Uses the real native planners,
// Chat template, completion FIM/seed/multi-prompt and visual token expansion.
// Publishes no public object, does not reserve RPM, and never invokes a model.
ObjectReply MeasureBatchInput(ObjectStore& objects, std::string_view endpoint,
                              const BatchInputRow& row,
                              const io::Json& prepared,
                              const ChatPlanConfig& config,
                              const BinaryAccess& access,
                              const BatchTokenCodec& codec,
                              BatchInputBudget* output);
}  // namespace q4t::server
