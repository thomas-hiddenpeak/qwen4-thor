// Real entry validation, deliberately no weights, owner or CUDA allocation.
#include "q4t/model/model.h"
#include "q4t/mtp/mtp.h"

#include <climits>
#include <iostream>
#include <stdexcept>
#include <string_view>

int main() {
  using namespace q4t::model;
  try {
    Model m;
    m.cfg.max_seq = 2;
    m.cfg.max_prefill = 2;
    m.cfg.max_len = 8;
    int cases = 0;
    auto check = [&](q4t::Status status, std::string_view expected) {
      if (status.ok() || status.message() != expected)
        throw std::runtime_error("unexpected status: " + status.message());
      ++cases;
    };
    for (int slot : {-1, 2})
      check(ModelDecodeStep(m, 1, 0, nullptr, nullptr, nullptr, nullptr, slot),
            "ModelDecodeStep: seq_id exceeds max_seq");
    check(ModelDecodeStep(m, 1, 0, nullptr, nullptr, nullptr),
          "ModelDecodeStep: upload owner missing");
    check(ModelDecodeStep(m, 1, -1, nullptr, nullptr, nullptr),
          "ModelDecodeStep: position < 0");
    check(ModelDecodeStep(m, 1, 8, nullptr, nullptr, nullptr),
          "ModelDecodeStep: position exceeds max_len");
    for (int slot : {-1, 2})
      check(ModelForward(m, nullptr, 1, nullptr, nullptr, nullptr, slot),
            "ModelForward: seq_id exceeds max_seq");
    check(ModelForward(m, nullptr, 3, nullptr, nullptr),
          "ModelForward: T exceeds max_prefill");
    check(ModelDecodeBatch(m, nullptr, 1, 0, nullptr, 0, nullptr, nullptr),
          "ModelDecodeBatch: upload owner missing");
    check(ModelDecodeBatch(m, nullptr, 1, 0, nullptr, 0, nullptr, nullptr,
                           nullptr, false, 2),
          "ModelDecodeBatch: seq_id exceeds max_seq");
    const int slots[] = {0, 1};
    const int bad_slot[] = {2};
    const int one[] = {1};
    const int huge[] = {INT_MAX, 1};
    const int zero[] = {0, 0};
    const int invalid_base[] = {INT_MAX};
    m.cfg.max_len = INT_MAX;
    m.cfg.max_prefill = INT_MAX;
    check(ModelPrefillBatch(m, nullptr, huge, slots, 2, nullptr, nullptr),
          "ModelPrefillBatch: Ttot exceeds max_prefill");
    check(ModelPrefillBatch(m, nullptr, one, bad_slot, 1, nullptr, nullptr),
          "ModelPrefillBatch: seq_id exceeds max_seq");
    check(ModelPrefillBatch(m, nullptr, one, slots, 1, nullptr, nullptr),
          "ModelPrefillBatch: upload owner missing");
    check(ModelMixedBatch(m, nullptr, one, slots, invalid_base, nullptr, 1,
                          nullptr, nullptr),
          "ModelMixedBatch: position range exceeds max_len");
    check(ModelMixedBatch(m, nullptr, huge, slots, zero, nullptr, 2,
                          nullptr, nullptr),
          "ModelMixedBatch: Ttot exceeds max_prefill");
    check(ModelMixedBatch(m, nullptr, one, slots, zero, nullptr, 1,
                          nullptr, nullptr),
          "ModelMixedBatch: upload owner missing");
    q4t::mtp::MtpModel draft;
    draft.cfg.max_prefill = 2;
    draft.max_seq = 2;
    check(q4t::mtp::MtpDraftExtend(draft, nullptr, nullptr, nullptr, 3,
                                  nullptr, nullptr, nullptr, 2),
          "MtpDraftExtend: seq_id out of range");
    check(q4t::mtp::MtpDraftExtend(draft, nullptr, nullptr, nullptr, 3,
                                  nullptr, nullptr, nullptr, 0),
          "MtpDraftExtend: upload owner missing");
    m.cfg.max_len = 1;
    check(ModelForward(m, nullptr, 2, nullptr, nullptr),
          "ModelForward: T exceeds max_len");
    std::cout << "PASS " << cases << " real entry checks; no model/GPU work\n";
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
