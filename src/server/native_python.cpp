// CPython is loaded only by the disposable worker process.
#include "native_python.h"

#include <dlfcn.h>

#include <cstddef>
#include <string>

#include "q4t/server/file_document_worker.h"
#include "q4t/server/workspace_python.h"

namespace q4t::server::detail {
namespace {
// Only these CPython 3.12 C ABI entry points cross the dynamic boundary.
// The model process can start even when the optional runtime is absent.
struct PyObject;
using PySize = std::ptrdiff_t;

template <class T>
bool Symbol(void* library, const char* name, T* out) {
  *out = reinterpret_cast<T>(dlsym(library, name));
  return *out != nullptr;
}

struct Python {
  void (*initialize)(int);
  void (*set_path)(const wchar_t*);
  PyObject* (*module)(const char*);
  PyObject* (*globals)(PyObject*);
  PyObject* (*unicode)(const char*, PySize);
  const char* (*utf8)(PyObject*, PySize*);
  int (*set)(PyObject*, const char*, PyObject*);
  PyObject* (*get)(PyObject*, const char*);
  int (*run)(const char*, void*);
  void (*decref)(PyObject*);

  bool Load() {
    void* library = dlopen("libpython3.12.so.1.0", RTLD_NOW | RTLD_GLOBAL);
    if (!library) return false;
    for (const auto* flag : {"Py_IsolatedFlag", "Py_IgnoreEnvironmentFlag",
                             "Py_NoSiteFlag", "Py_DontWriteBytecodeFlag"}) {
      int* setting = nullptr;
      if (!Symbol(library, flag, &setting)) return false;
      *setting = 1;
    }
    return Symbol(library, "Py_InitializeEx", &initialize) &&
           Symbol(library, "Py_SetPath", &set_path) &&
           Symbol(library, "PyImport_AddModule", &module) &&
           Symbol(library, "PyModule_GetDict", &globals) &&
           Symbol(library, "PyUnicode_FromStringAndSize", &unicode) &&
           Symbol(library, "PyUnicode_AsUTF8AndSize", &utf8) &&
           Symbol(library, "PyDict_SetItemString", &set) &&
           Symbol(library, "PyDict_GetItemString", &get) &&
           Symbol(library, "PyRun_SimpleStringFlags", &run) &&
           Symbol(library, "Py_DecRef", &decref);
  }
};
}  // namespace

bool RunIsolatedPython(std::string_view request, std::string* result) {
  Python python{};
  if (!python.Load()) return false;
  python.set_path(L"/usr/lib/python3.12:/usr/lib/python3.12/lib-dynload");
  python.initialize(0);
  if (python.run(kWorkspacePython, nullptr) != 0) return false;
  auto* main = python.module("__main__");
  auto* globals = main ? python.globals(main) : nullptr;
  if (!globals) return false;
  auto* payload = python.unicode(request.data(), request.size());
  if (!payload) return false;
  const bool bound = python.set(globals, "_request", payload) == 0;
  python.decref(payload);
  if (!bound || !EnterNativeDocumentSandbox()) return false;

  // No request content is parsed or evaluated until the kernel filter is
  // active.
  if (python.run("_result = json.dumps(evaluate(json.loads(_request)), "
                 "ensure_ascii=True, separators=(',', ':'))",
                 nullptr) != 0)
    return false;
  auto* reply = python.get(globals, "_result");
  PySize size = 0;
  const char* bytes = reply ? python.utf8(reply, &size) : nullptr;
  if (!bytes || size <= 0 || size > (32 << 20)) return false;
  result->assign(bytes, static_cast<size_t>(size));
  return true;
}
}  // namespace q4t::server::detail
