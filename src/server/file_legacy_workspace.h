#pragma once

#include <string>

namespace q4t::server::legacy_detail {
// All methods run in the owned codec/cleanup child. A dedicated root has a
// nonblocking lease across conversion, cleanup, and abnormal termination.
class Workspace {
 public:
  Workspace() = default;
  ~Workspace();
  Workspace(const Workspace&) = delete;
  Workspace& operator=(const Workspace&) = delete;
  bool Open(std::string root);
  bool Clean();
  bool Create(const std::string& token);
  // One-shot decoder keeps the lease until _exit closes the whole process.
  void KeepLeaseUntilExit() { keep_lease_ = true; }
  int Root() const { return root_; }
  int Directory() const { return directory_; }
  const std::string& Path() const { return path_; }

 private:
  std::string root_path_, path_;
  int root_ = -1, lease_ = -1, directory_ = -1;
  bool keep_lease_ = false;
};
}  // namespace q4t::server::legacy_detail
