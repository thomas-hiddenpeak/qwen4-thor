// Standalone, pure-host collector contracts. No CUDA, model, or sleeps.
// Build after the stage's first HTTP quality test, for example with:
// g++ -std=c++23 -Wall -Wextra -pthread -Iinclude <this file>
//     src/trace/mtp_cycle_timing.cpp src/io/json.cpp -o <stage>/host-test
#include "q4t/io/json.h"
#include "q4t/trace/mtp_cycle_timing.h"

#include <unistd.h>

#include <array>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <exception>
#include <functional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace {

using q4t::io::Json;
using q4t::trace::CycleDetail;
using q4t::trace::CyclePhase;
using q4t::trace::MtpCycleScope;
using q4t::trace::MtpCycleSpan;
using q4t::trace::MtpCycleStep;
using q4t::trace::MtpCycleTiming;

constexpr std::string_view kPrefix = "[q4t][mtp_cycle_timing] ";
// The final engine marker list is shared only as a protocol fixture: this
// test does not call model/scheduler code or reproduce numerical computation.
constexpr std::array kStepMarks = {
    "submit", "scheduler_pick", "model_lock_acquired", "engine_begin",
    "draft_end", "verify_pack_end", "verify_model_end", "verify_readback_end",
    "accept_end", "extend_pack_end", "extend_gather_end", "extend_model_end",
    "extend_readback_end", "engine_end", "scheduler_finish",
    "request_wake", "emit_begin", "emit_end", "advance_end"};
constexpr std::array kRequestMarks = {
    "timing_setup_begin", "timing_setup_end", "main_prefill_finished",
    "init_finished", "first_content_write_begin", "first_content_write_end",
    "request_end"};

void Require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

// A draining reader prevents large capacity-failure records from blocking
// fprintf. Capture is process-wide; only one test owns stderr at a time.
class StderrCapture {
 public:
  StderrCapture() {
    std::fflush(stderr);
    int fds[2];
    Require(pipe(fds) == 0, "pipe failed");
    saved_ = dup(STDERR_FILENO);
    if (saved_ < 0 || dup2(fds[1], STDERR_FILENO) < 0) {
      if (saved_ >= 0) close(saved_);
      close(fds[0]);
      close(fds[1]);
      throw std::runtime_error("stderr redirect failed");
    }
    close(fds[1]);
    try {
      reader_ = std::thread([this, fd = fds[0]] {
        char buffer[16384];
        for (;;) {
          const ssize_t count = read(fd, buffer, sizeof(buffer));
          if (count > 0) {
            text_.append(buffer, static_cast<size_t>(count));
          } else if (count == 0) {
            break;
          } else if (errno != EINTR) {
            read_error_ = errno;
            break;
          }
        }
        close(fd);
      });
    } catch (...) {
      dup2(saved_, STDERR_FILENO);
      close(saved_);
      saved_ = -1;
      close(fds[0]);
      throw;
    }
  }
  ~StderrCapture() { Restore(); }
  StderrCapture(const StderrCapture&) = delete;
  StderrCapture& operator=(const StderrCapture&) = delete;

  std::string Finish() {
    Restore();
    Require(read_error_ == 0 && restore_error_ == 0, "capture I/O failed");
    return std::move(text_);
  }

 private:
  void Restore() noexcept {
    if (saved_ < 0) return;
    std::fflush(stderr);
    if (dup2(saved_, STDERR_FILENO) < 0) {
      restore_error_ = errno;
      close(STDERR_FILENO);  // Also release the writer before joining.
    }
    close(saved_);
    saved_ = -1;
    if (reader_.joinable()) reader_.join();
  }
  int saved_ = -1;
  int read_error_ = 0;
  int restore_error_ = 0;
  std::thread reader_;
  std::string text_;
};

std::vector<Json> Capture(const std::function<void()>& action) {
  StderrCapture capture;
  action();
  const std::string text = capture.Finish();
  std::vector<Json> records;
  size_t begin = 0;
  while (begin < text.size()) {
    const size_t end = text.find('\n', begin);
    Require(end != std::string::npos, "unterminated collector record");
    const std::string_view line(text.data() + begin, end - begin);
    Require(line.starts_with(kPrefix), "unexpected stderr record");
    Json value;
    q4t::io::JsonParseLimits limits;
    limits.reject_duplicate_keys = true;
    const auto status = q4t::io::ParseJson(
        std::string(line.substr(kPrefix.size())), &value, false, limits);
    Require(status.ok() && value.IsObject(), "invalid collector JSON");
    records.push_back(std::move(value));
    begin = end + 1;
  }
  return records;
}

const Json& Field(const Json& object, std::string_view name,
                  Json::Type type) {
  const Json* value = object.Find(std::string(name));
  Require(value && value->type == type,
          "missing/wrong field: " + std::string(name));
  return *value;
}
const std::vector<Json>& Array(const Json& object, std::string_view name) {
  return Field(object, name, Json::Type::kArray).array;
}
bool Boolean(const Json& object, const std::string& name) {
  return Field(object, name, Json::Type::kBool).boolean;
}
std::string String(const Json& object, const std::string& name) {
  return Field(object, name, Json::Type::kString).str;
}
int64_t Integer(const Json& object, const std::string& name) {
  const auto& value = Field(object, name, Json::Type::kNumber);
  Require(std::isfinite(value.number) && std::floor(value.number) == value.number,
          "non-integer: " + name);
  return value.AsInt();
}
void Nonnegative(const Json& object, const std::string& name) {
  const double value = Field(object, name, Json::Type::kNumber).number;
  Require(std::isfinite(value) && value >= 0, "invalid time: " + name);
}

void StartRequest(MtpCycleTiming& timing) {
  timing.MarkRequest("main_prefill_finished");
  timing.MarkRequest("init_finished");
}
void FinishRequest(MtpCycleTiming& timing, int generated = 3, int steps = 1,
                   bool success = true) {
  timing.MarkRequest("first_content_write_begin");
  timing.FinishFirstContent();
  timing.MarkRequest("request_end");
  timing.Finish(generated, steps, false, "length", "none", success);
}
void CompleteStep(MtpCycleStep* step, int accepted = 2, int generated = -1,
                  std::string_view omit = {}) {
  Require(step != nullptr, "missing step");
  for (const char* name : kStepMarks)
    if (name != omit) step->Mark(name);
  step->SetEngineResult(accepted, accepted + 1, 4, accepted + 1);
  if (generated < 0) generated = accepted + 1;
  step->SetDelivery(generated, generated, generated);
}
Json Scenario(const std::function<void(MtpCycleTiming&)>& action,
              int max_tokens = 16, const std::string& id = "host-contract") {
  auto records = Capture([&] {
    MtpCycleTiming timing(id, 1024, max_tokens, 3,
                         MtpCycleTiming::Clock::now());
    StartRequest(timing);
    action(timing);
  });
  Require(records.size() == 1, "expected one request record");
  return std::move(records[0]);
}
void Valid(const Json& record) {
  Require(Boolean(record, "complete") && Boolean(record, "valid") &&
              Boolean(record, "success") && String(record, "error").empty(),
          "expected complete valid success");
}
void Invalid(const Json& record, const std::string& reason) {
  Require(!Boolean(record, "valid") && String(record, "error") == reason,
          "expected invalid: " + reason);
}
int64_t Calls(const Json& step, const std::string& phase,
               const std::string& detail) {
  const Json* found = nullptr;
  for (const auto& item : Array(step, "details")) {
    if (String(item, "phase") == phase && String(item, "name") == detail) {
      Require(!found, "duplicate detail");
      found = &item;
    }
  }
  Require(found, "missing detail");
  Nonnegative(*found, "host_ms");
  return Integer(*found, "calls");
}
void Span(CycleDetail detail) { MtpCycleSpan span(detail); }

template <size_t N>
void Marks(const Json& object, const std::string& field,
           const std::array<const char*, N>& names) {
  const auto& values = Array(object, field);
  Require(values.size() == names.size(), "marker count");
  double previous = 0;
  for (size_t i = 0; i < names.size(); ++i) {
    Require(String(values[i], "name") == names[i], "marker order/name");
    Nonnegative(values[i], "at_ms");
    const double now = values[i].GetNumber("at_ms");
    Require(now >= previous, "nonmonotonic host markers");
    previous = now;
  }
}

void InactiveNoOutput() {
  const auto records = Capture([] {
    Span(CycleDetail::kLinearAlloc);
    MtpCycleScope off(nullptr);
    off.SetPhase(CyclePhase::kVerify);
    Span(CycleDetail::kLinearFree);
  });
  Require(records.empty(), "inactive spans emitted output");
}

void NormalSchema() {
  const Json record = Scenario([](auto& timing) {
    auto* step = timing.BeginStep(1024, 3);
    {
      MtpCycleScope scope(step);
      scope.SetPhase(CyclePhase::kVerify);
      for (int i = 0; i < 48; ++i) Span(CycleDetail::kVerifyMoeCounts);
      for (int i = 0; i < 216; ++i) {
        Span(CycleDetail::kLinearAlloc);
        Span(CycleDetail::kLinearFree);
      }
    }
    CompleteStep(step);
    FinishRequest(timing);
  });
  Valid(record);
  Require(Integer(record, "schema_version") == 1 &&
              Integer(record, "prompt_tokens") == 1024 &&
              Integer(record, "max_tokens") == 16 && Integer(record, "k") == 3 &&
              Integer(record, "max_seq") == 1 &&
              Integer(record, "step_capacity") == 16 &&
              Integer(record, "generated") == 3 &&
              Integer(record, "mtp_steps") == 1, "request scalar schema");
  Require(Boolean(record, "text_only") && Boolean(record, "diagnostic_only") &&
              !Boolean(record, "contains_cuda_events") &&
              !Boolean(record, "host_details_additive") &&
              !Boolean(record, "plain_tail"), "request flags");
  Require(String(record, "clock") == "steady_clock" &&
              String(record, "host_origin") == "handle_chat_entry" &&
              String(record, "unit") == "ms" &&
              String(record, "finish_reason") == "length" &&
              String(record, "fallback") == "none", "request strings");
  Nonnegative(record, "setup_host_ms");
  Nonnegative(record, "report_host_ms");
  Marks(record, "request_marks", kRequestMarks);
  const auto& steps = Array(record, "steps");
  Require(steps.size() == 1, "normal step count");
  const auto& step = steps[0];
  Marks(step, "marks", kStepMarks);
  Require(Integer(step, "index") == 0 && Integer(step, "position") == 1024 &&
              Integer(step, "accepted_drafts") == 2 &&
              Integer(step, "returned") == 3 && Integer(step, "verify_rows") == 4 &&
              Integer(step, "extend_rows") == 3 &&
              Integer(step, "generated") == 3 &&
              Integer(step, "token_piece_writes") == 3 &&
              Integer(step, "nonempty_writes") == 3, "step fields");
  Require(Array(step, "details").size() == 44, "detail matrix shape");
  Require(Calls(step, "verify", "verify_moe_counts") == 48 &&
              Calls(step, "verify", "linear_alloc") == 216 &&
              Calls(step, "verify", "linear_free") == 216 &&
              Calls(step, "draft", "linear_alloc") == 0, "detail counts");
}

void TlsNestedRestore() {
  const auto records = Capture([] {
    MtpCycleTiming outer("outer", 1024, 16, 3, MtpCycleTiming::Clock::now());
    MtpCycleTiming inner("inner", 1024, 16, 3, MtpCycleTiming::Clock::now());
    StartRequest(outer);
    StartRequest(inner);
    auto* a = outer.BeginStep(1024, 3);
    auto* b = inner.BeginStep(1024, 3);
    Span(CycleDetail::kLinearAlloc);  // No active TLS despite live collectors.
    {
      MtpCycleScope scope(a);
      scope.SetPhase(CyclePhase::kVerify);
      Span(CycleDetail::kLinearAlloc);
      {
        MtpCycleScope nested(b);
        nested.SetPhase(CyclePhase::kExtend);
        Span(CycleDetail::kLinearAlloc);
      }
      {
        MtpCycleScope disabled(nullptr);
        Span(CycleDetail::kLinearAlloc);
      }
      {
        MtpCycleScope same_step(a);
        same_step.SetPhase(CyclePhase::kAccept);
        Span(CycleDetail::kLinearAlloc);
      }
      Span(CycleDetail::kLinearAlloc);
    }
    Span(CycleDetail::kLinearAlloc);
    CompleteStep(a);
    CompleteStep(b);
    FinishRequest(outer);
    FinishRequest(inner);
  });
  Require(records.size() == 2, "nested request count");
  for (const auto& record : records) {
    Valid(record);
    const auto& step = Array(record, "steps")[0];
    const bool outer = String(record, "response_id") == "outer";
    Require(Calls(step, "verify", "linear_alloc") == (outer ? 2 : 0) &&
                Calls(step, "extend", "linear_alloc") == (outer ? 0 : 1) &&
                Calls(step, "accept", "linear_alloc") == (outer ? 1 : 0),
            "nested/null TLS restoration");
  }
}

void TlsThreadIsolation() {
  const auto records = Capture([] {
    MtpCycleTiming timing("parent-thread", 1024, 16, 3,
                         MtpCycleTiming::Clock::now());
    StartRequest(timing);
    auto* step = timing.BeginStep(1024, 3);
    {
      MtpCycleScope scope(step);
      scope.SetPhase(CyclePhase::kVerify);
      std::exception_ptr worker_error;
      std::thread worker([&] {
        try {
          // A child thread must not inherit the parent's active request.
          for (int i = 0; i < 5; ++i) Span(CycleDetail::kLinearFree);
          MtpCycleTiming child("child-thread", 1024, 16, 3,
                               MtpCycleTiming::Clock::now());
          StartRequest(child);
          auto* child_step = child.BeginStep(1024, 3);
          {
            MtpCycleScope child_scope(child_step);
            child_scope.SetPhase(CyclePhase::kExtend);
            Span(CycleDetail::kLinearFree);
          }
          CompleteStep(child_step);
          FinishRequest(child);
        } catch (...) {
          worker_error = std::current_exception();
        }
      });
      worker.join();
      if (worker_error) std::rethrow_exception(worker_error);
      Span(CycleDetail::kLinearFree);
    }
    CompleteStep(step);
    FinishRequest(timing);
  });
  Require(records.size() == 2, "thread request count");
  for (const auto& record : records) {
    Valid(record);
    const auto& step = Array(record, "steps")[0];
    const bool parent = String(record, "response_id") == "parent-thread";
    Require(Calls(step, "verify", "linear_free") == (parent ? 1 : 0) &&
                Calls(step, "extend", "linear_free") == (parent ? 0 : 1),
            "cross-thread TLS leaked");
  }
}

void PhaseCapturedAtEntry() {
  const Json record = Scenario([](auto& timing) {
    auto* step = timing.BeginStep(1024, 3);
    {
      MtpCycleScope scope(step);
      scope.SetPhase(CyclePhase::kDraft);
      {
        MtpCycleSpan span(CycleDetail::kMtpForward);
        scope.SetPhase(CyclePhase::kVerify);
      }
    }
    CompleteStep(step);
    FinishRequest(timing);
  });
  Valid(record);
  Require(Calls(Array(record, "steps")[0], "draft", "mtp_forward") == 1 &&
              Calls(Array(record, "steps")[0], "verify", "mtp_forward") == 0,
          "span changed phase at exit");
}

void FixedMarkerProtocol() {
  for (const char* omitted : kStepMarks) {
    const auto record = Scenario([&](auto& timing) {
      CompleteStep(timing.BeginStep(1024, 3), 2, 3, omitted);
      FinishRequest(timing);
    });
    Invalid(record, "incomplete");
    Require(!Boolean(record, "complete"), "missing step marker accepted");
  }
  for (bool duplicate : {false, true}) {
    const auto record = Scenario([&](auto& timing) {
      auto* step = timing.BeginStep(1024, 3);
      auto names = kStepMarks;
      if (duplicate)
        names[1] = names[0];
      else
        std::swap(names[0], names[1]);
      for (const char* name : names) step->Mark(name);
      step->SetEngineResult(2, 3, 4, 3);
      step->SetDelivery(3, 3, 3);
      FinishRequest(timing);
    });
    Invalid(record, "incomplete");
  }
  const auto request = Scenario([](auto& timing) {
    CompleteStep(timing.BeginStep(1024, 3));
    // A successful result without the first-content marker pair is not a
    // complete diagnostic record, even though numerical work completed.
    timing.MarkRequest("request_end");
    timing.Finish(3, 1, false, "length", "none", true);
  });
  Invalid(request, "request_marks_incomplete");
  Require(!Boolean(request, "complete"), "missing request markers accepted");
}

void StepCapacity() {
  bool continued = false;
  const Json record = Scenario([&](auto& timing) {
    for (int i = 0; i < 1024; ++i)
      Require(timing.BeginStep(1024 + i, 3), "early step capacity");
    Require(timing.BeginStep(2048, 3) == nullptr, "missing step limit");
    Require(timing.BeginStep(2049, 3) == nullptr, "overflow mutated capacity");
    continued = true;
  }, 2048);
  Invalid(record, "step_capacity");
  Require(continued && Integer(record, "step_capacity") == 1024 &&
              Array(record, "steps").size() == 1024, "step cap continuation");
}

void MarkCapacities() {
  bool continued = false;
  auto request = Scenario([&](auto& timing) {
    for (int i = 0; i < 80; ++i) timing.MarkRequest("extra");
    continued = true;
  });
  Invalid(request, "request_mark_capacity");
  Require(continued && Array(request, "request_marks").size() == 64,
          "request mark cap continuation");
  continued = false;
  const auto step_record = Scenario([&](auto& timing) {
    auto* step = timing.BeginStep(1024, 3);
    for (int i = 0; i < 80; ++i) step->Mark("extra");
    continued = true;
  });
  Invalid(step_record, "step_mark_capacity");
  Require(continued && Array(Array(step_record, "steps")[0], "marks").size() == 64,
          "step mark cap continuation");
}

void IncompleteCannotPass() {
  for (int mode = 0; mode < 6; ++mode) {
    const auto record = Scenario([&](auto& timing) {
      auto* step = timing.BeginStep(1024, 3);
      if (mode == 2 || mode == 3) {
        CompleteStep(step, 2, 3, mode == 2 ? "engine_end" : "advance_end");
      } else if (mode == 4 || mode == 5) {
        for (const char* name : kStepMarks) step->Mark(name);
        if (mode == 4) step->SetEngineResult(2, 3, 4, 3);
        // mode 5 deliberately leaves both fields unset; SetDelivery before
        // engine would instead fail the delivery contract immediately.
      } else {
        CompleteStep(step);
      }
      if (mode != 0) {
        FinishRequest(timing, 3, 1, mode != 1);
      } else {
        timing.MarkRequest("first_content_write_begin");
        timing.FinishFirstContent();
        timing.MarkRequest("request_end");
      }
    });
    Invalid(record, "incomplete");
    Require(!Boolean(record, "complete"), "incomplete marked complete");
  }
}

void EngineContracts() {
  constexpr int bad[][4] = {{-1, 0, 4, 0}, {4, 5, 4, 5}, {2, 2, 4, 3},
                            {2, 3, 3, 3}, {2, 3, 4, 2}};
  for (const auto& fields : bad) {
    const auto record = Scenario([&](auto& timing) {
      auto* step = timing.BeginStep(1024, 3);
      step->SetEngineResult(fields[0], fields[1], fields[2], fields[3]);
    });
    Invalid(record, "engine_result_contract");
  }
}

void DeliveryContracts() {
  constexpr int bad[][3] = {{-1, 0, 0}, {4, 3, 3}, {3, -1, 0},
                            {2, 3, 2}, {3, 2, -1}, {3, 2, 3}};
  for (const auto& fields : bad) {
    const auto record = Scenario([&](auto& timing) {
      auto* step = timing.BeginStep(1024, 3);
      step->SetEngineResult(2, 3, 4, 3);
      step->SetDelivery(fields[0], fields[1], fields[2]);
    });
    Invalid(record, "delivery_contract");
  }
}

void AcceptedCountsAndClipping() {
  for (int accepted = 0; accepted <= 3; ++accepted) {
    for (int delivered = 0; delivered <= accepted + 1; ++delivered) {
      const auto record = Scenario([&](auto& timing) {
        auto* step = timing.BeginStep(1024, 3);
        CompleteStep(step, accepted, delivered);
        FinishRequest(timing, delivered);
      });
      Valid(record);
      const auto& step = Array(record, "steps")[0];
      Require(Integer(step, "returned") == accepted + 1 &&
                  Integer(step, "generated") == delivered, "delivery clipping");
    }
  }
}

void DuplicateResults() {
  const std::array reasons = {"duplicate_engine_result", "duplicate_delivery",
                              "duplicate_finish"};
  for (size_t mode = 0; mode < reasons.size(); ++mode) {
    const auto record = Scenario([&](auto& timing) {
      auto* step = timing.BeginStep(1024, 3);
      CompleteStep(step);
      FinishRequest(timing);
      if (mode == 0) step->SetEngineResult(2, 3, 4, 3);
      if (mode == 1) step->SetDelivery(3, 3, 3);
      if (mode == 2) timing.Finish(3, 1, false, "length", "none", true);
    });
    Invalid(record, reasons[mode]);
  }
}

void ShapeAndEnumContracts() {
  for (int mode = 0; mode < 4; ++mode) {
    const auto record = Scenario([&](auto& timing) {
      auto* step = timing.BeginStep(mode == 0 ? 1023 : 1024, mode == 1 ? 2 : 3);
      MtpCycleScope scope(step);
      if (mode == 2) scope.SetPhase(CyclePhase::kCount);
      if (mode == 3) Span(CycleDetail::kCount);
    });
    Invalid(record, mode < 2 ? "step_shape" :
                    mode == 2 ? "invalid_phase" : "invalid_detail");
  }
}

void RequestResultContracts() {
  constexpr int bad[][2] = {{-1, 1}, {17, 1}, {3, -1}, {3, 2}, {2, 1}};
  for (const auto& fields : bad) {
    const auto record = Scenario([&](auto& timing) {
      CompleteStep(timing.BeginStep(1024, 3));
      FinishRequest(timing, fields[0], fields[1]);
    });
    Invalid(record, "request_result_contract");
  }
}

void NullMarksAndFirstError() {
  const auto a = Scenario([](auto& timing) {
    timing.MarkRequest(nullptr);
    timing.MarkRequest("after_null");
    timing.BeginStep(0, 3);  // Must retain the first diagnostic failure.
  });
  Invalid(a, "null_request_mark");
  const auto b = Scenario([](auto& timing) {
    auto* step = timing.BeginStep(1024, 3);
    step->Mark(nullptr);
    step->Invalidate("later_failure");
  });
  Invalid(b, "null_step_mark");
}

void FirstContentIdempotent() {
  const auto record = Scenario([](auto& timing) {
    Require(!timing.FirstContentWritten(), "first content initially set");
    CompleteStep(timing.BeginStep(1024, 3));
    FinishRequest(timing);
    timing.FinishFirstContent();
    Require(timing.FirstContentWritten(), "first content missing");
  });
  Valid(record);
  Marks(record, "request_marks", kRequestMarks);
}

void StringEscaping() {
  const std::string special = "quote\" slash\\ newline\n tab\t ctrl\x01 中文";
  constexpr char kMark[] = "mark\"\\\n\t\x02";
  const auto record = Scenario([&](auto& timing) {
    CompleteStep(timing.BeginStep(1024, 3));
    timing.MarkRequest("first_content_write_begin");
    timing.FinishFirstContent();
    timing.MarkRequest("request_end");
    timing.Finish(3, 1, false, special, special, true);
  }, 16, special);
  Valid(record);
  Require(String(record, "response_id") == special &&
              String(record, "finish_reason") == special &&
              String(record, "fallback") == special, "string roundtrip");
  const auto invalid_mark = Scenario([&](auto& timing) {
    timing.MarkRequest(kMark);
    CompleteStep(timing.BeginStep(1024, 3));
    FinishRequest(timing);
  });
  Invalid(invalid_mark, "request_marks_incomplete");
  bool found = false;
  for (const auto& mark : Array(invalid_mark, "request_marks"))
    found |= String(mark, "name") == kMark;
  Require(found, "escaped marker lost");
}

}  // namespace

int main() {
  const std::vector<std::pair<const char*, std::function<void()>>> tests = {
      {"inactive_no_output", InactiveNoOutput},
      {"normal_schema_marks_counts", NormalSchema},
      {"tls_nested_null_restore", TlsNestedRestore},
      {"tls_thread_isolation", TlsThreadIsolation},
      {"phase_captured_at_entry", PhaseCapturedAtEntry},
      {"fixed_marker_protocol", FixedMarkerProtocol},
      {"step_capacity_continues", StepCapacity},
      {"mark_capacities_continue", MarkCapacities},
      {"incomplete_cannot_pass", IncompleteCannotPass},
      {"engine_contracts", EngineContracts},
      {"delivery_contracts", DeliveryContracts},
      {"accepted_counts_and_clipping", AcceptedCountsAndClipping},
      {"duplicate_results", DuplicateResults},
      {"shape_and_enum_contracts", ShapeAndEnumContracts},
      {"request_result_contracts", RequestResultContracts},
      {"null_marks_and_first_error", NullMarksAndFirstError},
      {"first_content_idempotent", FirstContentIdempotent},
      {"string_escaping", StringEscaping},
  };
  int failures = 0;
  for (const auto& [name, test] : tests) {
    try {
      test();
      std::printf("PASS %s\n", name);
    } catch (const std::exception& error) {
      ++failures;
      std::printf("FAIL %s: %s\n", name, error.what());
    }
  }
  std::printf("MTP_CYCLE_HOST contracts=%zu passed=%zu failed=%d gpu=0\n",
              tests.size(), tests.size() - static_cast<size_t>(failures), failures);
  return failures == 0 ? 0 : 1;
}
