#include "schema_idna.h"

#include <dlfcn.h>

#include <new>
#include <string>

namespace q4t::server {
namespace {
class IdnaLibrary {
 public:
  IdnaLibrary() {
    // A fixed system SONAME, local-only symbol scope, no request-supplied path.
    // Optional native library: an unavailable backend is a request dependency
    // error, not invalid user data, and never a model health/restart signal.
    handle_ = dlopen("libidn2.so.0", RTLD_NOW | RTLD_LOCAL);
    if (!handle_) return;
    using Version = const char* (*)(const char*);
    const auto version =
        reinterpret_cast<Version>(dlsym(handle_, "idn2_check_version"));
    if (version && version("2.3.0"))
      validate_ = reinterpret_cast<SchemaIdnaRegister>(
          dlsym(handle_, "idn2_register_u8"));
  }
  ~IdnaLibrary() {
    if (handle_) dlclose(handle_);
  }
  SchemaIdnaRegister Validator() const { return validate_; }

 private:
  void* handle_ = nullptr;
  SchemaIdnaRegister validate_ = nullptr;
};
}  // namespace

bool CheckSchemaIdnaLabel(std::string_view label, SchemaIdnaRegister validate) {
  if (!validate)
    throw SchemaDependencyUnavailable(
        "IDNA2008 hostname validation requires system libidn2 >= 2.3");
  if (label.empty() || label.size() > 63 || label.find('\0') != label.npos)
    return false;
  std::string lower(label);
  for (char& c : lower)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  // Register checks NFC, A-label roundtrip, IDNA2008 derived properties,
  // CONTEXTJ/CONTEXTO, leading marks and bidi. Passing no output avoids an
  // unnecessary result allocation. It never performs a DNS lookup.
  const int result = validate(
      nullptr, reinterpret_cast<const uint8_t*>(lower.c_str()), nullptr, 0);
  if (!result) return true;
  if (result == -100) throw std::bad_alloc();  // Public IDN2_MALLOC code.
  if (result <= -200 && result > -400)
    return false;  // Encoding/label validity.
  throw SchemaDependencyUnavailable("IDNA2008 backend failed validation");
}

bool CheckSchemaIdnaLabel(std::string_view label) {
  static const IdnaLibrary library;
  return CheckSchemaIdnaLabel(label, library.Validator());
}

}  // namespace q4t::server
