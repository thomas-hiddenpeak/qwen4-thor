#include <cstdio>
#include <stdexcept>
#include <string>

#include "q4t/server/suffix_causal.h"

using q4t::server::SuffixCausalChoice;
using q4t::server::SuffixCausalPlan;

void Require(bool value) {
  if (!value) throw std::runtime_error("causal contract failed");
}

int main() {
  int tests = 0;
  {
    SuffixCausalPlan plan;
    SuffixCausalChoice choice;
    size_t ordinal = 99;
    Require(!plan.Enabled());
    Require(!plan.AfterPrefill(10, 1, &choice, &ordinal).ok());
    Require(plan.Consumed() == 0);
    ++tests;
  }
  for (const std::string text : {
           "suffix-causal-v2 S1 10 N1", "suffix-causal-v1 S0 10 Q",
           "suffix-causal-v1 S1 10 BAD", "suffix-causal-v1 S1 -1 N1",
           "suffix-causal-v1 S1 208897 N1", "suffix-causal-v1 S1",
           "suffix-causal-v1 S1 10 N1 20"}) {
    SuffixCausalPlan plan;
    Require(!plan.Parse(text, true).ok());
    Require(!plan.Enabled() && plan.Size() == 0);
    ++tests;
  }
  {
    SuffixCausalPlan plan;
    Require(!plan.Parse("suffix-causal-v1 S0 10 Q", false).ok());
    Require(!plan.Parse(std::string(4097, 'x'), true).ok());
    Require(!plan.Load("relative", false).ok());
    ++tests;
  }
  {
    SuffixCausalPlan plan;
    std::string text = "suffix-causal-v1 S1\n";
    for (size_t i = 0; i <= SuffixCausalPlan::kMaxRequests; ++i)
      text += "10 N1\n";
    Require(!plan.Parse(text, true).ok() && !plan.Enabled());
    ++tests;
  }
  {
    SuffixCausalPlan plan;
    Require(plan.Parse("suffix-causal-v1 S1 10 N2 20 Q 30 R 40 N1",
                       true).ok());
    SuffixCausalChoice choice;
    size_t ordinal = 99;
    Require(!plan.AfterPrefill(10, 0, &choice, &ordinal).ok());
    Require(plan.Consumed() == 0 && ordinal == 99);
    Require(plan.AfterPrefill(10, 1, &choice, &ordinal).ok());
    Require(ordinal == 0 && !choice.query && !choice.apply);
    // A request scope ends (including cancellation); the next request gets
    // its own immutable choice, never the preceding request's mode.
    Require(plan.AfterPrefill(20, 2, &choice, &ordinal).ok());
    Require(ordinal == 1 && choice.query && !choice.apply);
    Require(plan.AfterPrefill(30, 3, &choice, &ordinal).ok());
    Require(ordinal == 2 && choice.query && choice.apply);
    Require(plan.AfterPrefill(40, 4, &choice, &ordinal).ok());
    Require(ordinal == 3 && !choice.query && !choice.apply);
    Require(!plan.AfterPrefill(40, 5, &choice, &ordinal).ok());
    Require(!plan.Parse("suffix-causal-v1 S1 10 N1", true).ok());
    ++tests;
  }
  {
    SuffixCausalPlan plan;
    Require(plan.Parse("suffix-causal-v1 S0 10 N1", false).ok());
    SuffixCausalChoice choice;
    size_t ordinal = 99;
    Require(!plan.AfterPrefill(11, 1, &choice, &ordinal).ok());
    Require(!plan.AfterPrefill(10, 2, &choice, &ordinal).ok());
    Require(ordinal == 99);  // Failure cannot reset or replay the plan.
    ++tests;
  }
  std::printf("{\"tests\":%d,\"failed\":0,\"skipped\":0}\n", tests);
}
