// Small non-allocating guards for serving resource ownership.
#pragma once

#include <exception>
#include <utility>

namespace q4t::server {

template <typename Function>
class ScopeExit {
 public:
  explicit ScopeExit(Function function) : function_(std::move(function)) {}
  ~ScopeExit() { Run(); }
  ScopeExit(const ScopeExit&) = delete;
  ScopeExit& operator=(const ScopeExit&) = delete;
  void Run() noexcept {
    if (!active_) return;
    active_ = false;
    function_();
  }
  void Release() noexcept { active_ = false; }

 private:
  Function function_;
  bool active_ = true;
};

template <typename Function>
class OnException {
 public:
  explicit OnException(Function function) : function_(std::move(function)) {}
  ~OnException() {
    if (std::uncaught_exceptions() > exceptions_) function_();
  }
  OnException(const OnException&) = delete;
  OnException& operator=(const OnException&) = delete;

 private:
  Function function_;
  const int exceptions_ = std::uncaught_exceptions();
};

}  // namespace q4t::server
