#pragma once

namespace q4t::server {

// Owns stderr for the lifetime of the standalone serve command. A slow
// collector may lose diagnostics once the bounded kernel queue fills.
class ServeDiagnostics {
 public:
  ServeDiagnostics();
  ~ServeDiagnostics();
  ServeDiagnostics(const ServeDiagnostics&) = delete;
  ServeDiagnostics& operator=(const ServeDiagnostics&) = delete;

 private:
  bool forwarding_ = false;
};

}  // namespace q4t::server
