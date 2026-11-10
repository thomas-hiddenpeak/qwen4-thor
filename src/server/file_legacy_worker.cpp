#include "q4t/server/file_legacy.h"

#include <dlfcn.h>
#include <fcntl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdlib>
#include <cstring>

#include <LibreOfficeKit/LibreOfficeKit.h>

#include "file_legacy_protocol.h"
#include "file_legacy_workspace.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
namespace {
using namespace legacy_detail;
bool ReadyReply(Ready ready) {
  return detail::WorkerTransfer(&ready, sizeof(ready), true);
}
bool ResultReply(Failure failure, Format format = Format::kText,
                 std::string_view bytes = {}) {
  Result result{kMagic, failure, format, bytes.size()};
  return detail::WorkerTransfer(&result, sizeof(result), true) &&
         detail::WorkerTransfer(const_cast<char*>(bytes.data()), bytes.size(),
                                true);
}
bool WriteAll(int file, std::string_view bytes) {
  while (!bytes.empty()) {
    const auto count = write(file, bytes.data(), bytes.size());
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) return false;
    bytes.remove_prefix(count);
  }
  return true;
}
std::string Url(std::string_view path) {
  constexpr char hex[] = "0123456789ABCDEF";
  std::string url = "file://";
  for (unsigned char c : path) {
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
        (c >= '0' && c <= '9') || c == '/' || c == '-' || c == '_' || c == '.')
      url += c;
    else {
      url += '%';
      url += hex[c >> 4];
      url += hex[c & 15];
    }
  }
  return url;
}
bool Environment(const Workspace& workspace) {
  // The exec entry starts with the parent's fixed minimal environment. Never
  // use the host user's profile, a display server, or GPU acceleration.
  for (const char* name : {"profile", "tmp", "cache"})
    if (mkdirat(workspace.Directory(), name, 0700)) return false;
  const auto tmp = workspace.Path() + "/tmp";
  const auto cache = workspace.Path() + "/cache";
  const auto profile = workspace.Path() + "/profile";
  return setenv("SAL_USE_VCLPLUGIN", "svp", 1) == 0 &&
         setenv("SAL_DISABLE_OPENCL", "1", 1) == 0 &&
         setenv("SAL_DISABLEGL", "1", 1) == 0 &&
         setenv("SAL_DISABLESKIA", "1", 1) == 0 &&
         setenv("MAX_CONCURRENCY", "1", 1) == 0 &&
         setenv("LC_ALL", "C.UTF-8", 1) == 0 && setenv("TZ", "UTC", 1) == 0 &&
         setenv("TMPDIR", tmp.c_str(), 1) == 0 &&
         setenv("XDG_CACHE_HOME", cache.c_str(), 1) == 0 &&
         setenv("XDG_CONFIG_HOME", profile.c_str(), 1) == 0;
}
bool Profile(const Workspace& workspace) {
  const auto path = workspace.Path() + "/profile/user";
  if (mkdirat(AT_FDCWD, path.c_str(), 0700)) return false;
  const int file =
      open((path + "/registrymodifications.xcu").c_str(),
           O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (file < 0) return false;
  ScopeExit cleanup([&] { close(file); });
  constexpr std::string_view policy =
      "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
      "<oor:items xmlns:oor=\"http://openoffice.org/2001/registry\">"
      "<item oor:path=\"/org.openoffice.Office.Common/Security/Scripting\">"
      "<prop oor:name=\"DisableMacrosExecution\" "
      "oor:op=\"fuse\"><value>true</value></prop>"
      "<prop oor:name=\"DisableActiveContent\" "
      "oor:op=\"fuse\"><value>true</value></prop>"
      "<prop oor:name=\"BlockUntrustedRefererLinks\" "
      "oor:op=\"fuse\"><value>true</value></prop>"
      "<prop oor:name=\"MacroSecurityLevel\" "
      "oor:op=\"fuse\"><value>3</value></prop>"
      "</item><item oor:path=\"/org.openoffice.Office.Common/Misc\">"
      "<prop oor:name=\"UseOpenCL\" oor:op=\"fuse\"><value>false</value></prop>"
      "</item><item oor:path=\"/org.openoffice.Office.Calc/Content/Update\">"
      "<prop oor:name=\"Link\" oor:op=\"fuse\"><value>1</value></prop>"
      "</item><item oor:path=\"/org.openoffice.Office.Writer/Content/Update\">"
      "<prop oor:name=\"Link\" oor:op=\"fuse\"><value>2</value></prop>"
      "</item><item oor:path=\"/org.openoffice.Office.Calc/Formula/Load\">"
      "<prop oor:name=\"OOXMLRecalcMode\" "
      "oor:op=\"fuse\"><value>1</value></prop>"
      "<prop oor:name=\"ODFRecalcMode\" oor:op=\"fuse\"><value>1</value></prop>"
      "</item></oor:items>";
  return WriteAll(file, policy) && fsync(file) == 0;
}
Failure Convert(const Workspace& workspace, Format* format) {
  constexpr char library[] = Q4T_SYSTEM_LIBREOFFICE_LIBRARY;
  struct stat info {};
  if (stat(library, &info) || !S_ISREG(info.st_mode) || info.st_uid != 0 ||
      (info.st_mode & (S_IWGRP | S_IWOTH)))
    return Failure::kDependency;
  void* runtime = dlopen(library, RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
  if (!runtime) return Failure::kDependency;
  using Initialize = LibreOfficeKit* (*)(const char*, const char*);
  const auto initialize =
      reinterpret_cast<Initialize>(dlsym(runtime, "libreofficekit_hook_2"));
  if (!initialize) return Failure::kDependency;
  const auto profile = Url(workspace.Path() + "/profile");
  auto* office = initialize(Q4T_SYSTEM_LIBREOFFICE_DIRECTORY, profile.c_str());
  if (!office || !office->pClass ||
      !LIBREOFFICEKIT_HAS(office, documentLoadWithOptions) ||
      !office->pClass->documentLoadWithOptions || !office->pClass->destroy)
    return Failure::kDependency;
  ScopeExit close_office([&] { office->pClass->destroy(office); });
  const auto source = Url(workspace.Path() + "/source");
  auto* document = office->pClass->documentLoadWithOptions(
      office, source.c_str(),
      "Batch=true,EnableMacrosExecution=false,MacroSecurityLevel=3,Timezone="
      "UTC");
  if (!document || !document->pClass) return Failure::kInvalid;
  if (!LIBREOFFICEKIT_DOCUMENT_HAS(document, getDocumentType) ||
      !document->pClass->getDocumentType || !document->pClass->destroy ||
      !document->pClass->saveAs)
    return Failure::kDependency;
  ScopeExit close_document([&] { document->pClass->destroy(document); });
  const int type = document->pClass->getDocumentType(document);
  // Stable LibreOfficeKit document type ABI: text, sheet, slides, drawing.
  const char* extension = nullptr;
  switch (type) {
    case 0:
      *format = Format::kText;
      extension = "odt";
      break;
    case 1:
      *format = Format::kSheet;
      extension = "ods";
      break;
    case 2:
      *format = Format::kSlides;
      extension = "odp";
      break;
    case 3:
      *format = Format::kDrawing;
      extension = "odg";
      break;
    default:
      return Failure::kInvalid;
  }
  const auto destination = Url(workspace.Path() + "/converted");
  if (!document->pClass->saveAs(document, destination.c_str(), extension,
                                nullptr))
    return Failure::kInvalid;
  return Failure::kNone;
}
int Run(int argc, char** argv) {
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const rlimit cpu{120, 120};
  if (setrlimit(RLIMIT_CPU, &cpu)) return 2;
  Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.magic != kMagic || !request.root_bytes ||
      request.root_bytes > 4096 ||
      (request.operation != Operation::kConvert &&
       request.operation != Operation::kClean) ||
      request.source_bytes > kFileInputBytes ||
      request.output_bytes > kFileInputBytes)
    return 3;
  std::string root(request.root_bytes, '\0');
  if (!detail::WorkerTransfer(root.data(), root.size(), false)) return 3;
  Workspace workspace;
  if (!workspace.Open(std::move(root)) || !workspace.Clean())
    return ReadyReply(Ready::kStorage) ? 0 : 3;
  workspace.KeepLeaseUntilExit();
  if (request.operation == Operation::kClean)
    return ReadyReply(Ready::kClean) ? 0 : 3;
  if (!request.source_bytes || !request.output_bytes ||
      !workspace.Create(std::string(request.token, sizeof(request.token))))
    return ReadyReply(Ready::kStorage) ? 0 : 3;
  const rlimit file_size{std::max(request.source_bytes, request.output_bytes),
                         std::max(request.source_bytes, request.output_bytes)};
  if (setrlimit(RLIMIT_FSIZE, &file_size) || chdir(workspace.Path().c_str()))
    return ReadyReply(Ready::kStorage) ? 0 : 3;
  const int listener =
      detail::EnterLegacyDocumentSandbox(workspace.Directory());
  if (listener < 0) return ReadyReply(Ready::kSandbox) ? 0 : 3;
  if (!ReadyReply(Ready::kReady) || !detail::SendWorkerIoListener(listener))
    return 3;
  close(listener);
  int source =
      openat(workspace.Directory(), "source",
             O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (source < 0) return 3;
  ScopeExit close_source([&] {
    if (source >= 0) close(source);
  });
  std::array<char, 65536> buffer;
  for (uint64_t left = request.source_bytes; left;) {
    const size_t size = std::min<uint64_t>(left, buffer.size());
    if (!detail::WorkerTransfer(buffer.data(), size, false) ||
        !WriteAll(source, std::string_view(buffer.data(), size)))
      return 3;
    left -= size;
  }
  if (fsync(source)) return ResultReply(Failure::kStorage) ? 0 : 3;
  close(source);
  source = -1;
  if (!Environment(workspace) || !Profile(workspace))
    return ResultReply(Failure::kStorage) ? 0 : 3;
  Format format;
  const auto converted = Convert(workspace, &format);
  if (converted != Failure::kNone) return ResultReply(converted) ? 0 : 3;
  const int output = openat(workspace.Directory(), "converted",
                            O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  if (output < 0) return ResultReply(Failure::kStorage) ? 0 : 3;
  ScopeExit close_output([&] { close(output); });
  struct stat info {};
  if (fstat(output, &info) || !S_ISREG(info.st_mode) ||
      info.st_uid != getuid() || info.st_nlink != 1 || info.st_size <= 0)
    return ResultReply(Failure::kStorage) ? 0 : 3;
  if (static_cast<uint64_t>(info.st_size) > request.output_bytes)
    return ResultReply(Failure::kCapacity) ? 0 : 3;
  Result result{kMagic, Failure::kNone, format,
                static_cast<uint64_t>(info.st_size)};
  if (!detail::WorkerTransfer(&result, sizeof(result), true)) return 3;
  for (uint64_t left = result.bytes; left;) {
    const auto count =
        read(output, buffer.data(), std::min<uint64_t>(left, buffer.size()));
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0 || !detail::WorkerTransfer(buffer.data(), count, true))
      return 3;
    left -= count;
  }
  return 0;
}
}  // namespace

int MaybeRunFileLegacyWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--file-legacy-worker")) return -1;
  try {
    // Run finishes explicit document/office cleanup and complete transport.
    // This is a one-shot owned process: avoid unrelated library atexit/global
    // destructors after office teardown. Kernel exit releases the retained
    // root lease; the parent still requires EOF AND normal zero exit.
    _exit(Run(argc, argv));
  } catch (...) {
    _exit(3);
  }
}
}  // namespace q4t::server
