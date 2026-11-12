#include "q4t/service/service_cli.h"

#include <sys/stat.h>
#include <unistd.h>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/trace/sha256.h"

namespace q4t::service {
namespace {
namespace fs = std::filesystem;

// Runs with the pinned interpreter's isolated mode. Validate the source bytes
// before importing any package code. No PATH lookup, shell, GPU call or model
// action is involved in dispatch. Immutable package verification follows.
constexpr const char* kBootstrap = R"py(
import hashlib,json,os,stat,sys
from pathlib import Path
sys.dont_write_bytecode=True
def need(ok):
    if not ok: raise ValueError('service source identity changed')
def unique(pairs):
    result={}
    for k,v in pairs:
        need(k not in result);result[k]=v
    return result
def read(path,maximum):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_size<=maximum)
        with os.fdopen(os.dup(fd),'rb') as source: raw=source.read(maximum+1)
        need(len(raw)==info.st_size)
        return raw
    finally:os.close(fd)
try:
    root=Path(sys.argv[1]);digest=sys.argv[2];mode=sys.argv[3];engine_digest=sys.argv[4]
    raw=read(root/'manifest.json',2*1024**2)
    need(hashlib.sha256(raw).hexdigest()==digest)
    manifest=json.loads(raw,object_pairs_hook=unique)
    need(Path(sys.executable).resolve()==Path(manifest['python']['path']))
    files=manifest['files'];need(type(files) is dict and len(files)<=4096)
    for name,wanted in files.items():
        need(type(name) is str and not Path(name).is_absolute() and
             not {'','.','..'}.intersection(name.split('/')))
        if not name.startswith('source/'):continue
        path=root/name;need(path.resolve()==path)
        need(hashlib.sha256(read(path,4*1024**2)).hexdigest()==wanted)
    need('source/tools/serve_recovery/service.py' in files)
    need((mode=='package' and manifest['profile'].startswith('q4t-service-recovery-v')) or
         (mode=='tools' and manifest['profile']=='q4t-service-tools-v1'))
    sys.path.insert(0,str(root/'source'))
    from tools.serve_recovery import bundle,service
    if mode=='package':
        bundle.verify(root,digest)
        from tools.serve_service import package
        need(package.read(root/'engine/manifest.json')['files']['q4t']==engine_digest)
except Exception as error:
    print(json.dumps(dict(status='FAILED',category=type(error).__name__,
          code='service_entry_identity_invalid')),file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(service.main(sys.argv[5:],workspace=manifest['repository'],**(
    dict(package=root,digest=digest) if mode=='package' else {})))
)py";

void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

std::string Hash(const fs::path& path) {
  Require(fs::is_regular_file(path), "service identity needs a regular file");
  trace::Digest digest;
  Require(trace::HashFile(path.string(), &digest).ok(),
          "cannot read service identity");
  return trace::HexDigest(digest);
}

io::Json Manifest(const fs::path& path) {
  struct stat info {};
  Require(lstat(path.c_str(), &info) == 0 && S_ISREG(info.st_mode) &&
              info.st_size > 0 && info.st_size <= 2 * 1024 * 1024,
          "invalid service entry manifest");
  std::ifstream input(path, std::ios::binary);
  std::string raw(static_cast<size_t>(info.st_size), '\0');
  Require(input.read(raw.data(), raw.size()).good() && input.peek() == EOF,
          "service manifest changed while reading");
  io::Json value;
  io::JsonParseLimits limits;
  limits.max_depth = 16;
  limits.max_values = 16384;
  limits.max_string_bytes = 2 * 1024 * 1024;
  limits.reject_duplicate_keys = true;
  Require(io::ParseJson(raw, &value, false, limits).ok() && value.IsObject(),
          "invalid service entry manifest JSON");
  return value;
}

}  // namespace

int RunService(int argc, char** argv) {
  try {
    const fs::path executable = fs::canonical("/proc/self/exe");
    const fs::path directory = executable.parent_path();
    fs::path root;
    std::string mode = "package";
    if (fs::exists(directory / "service/manifest.json")) {
      root = fs::canonical(directory / "service");
    } else if (directory.filename() == "engine" &&
               fs::exists(directory.parent_path() / "manifest.json")) {
      root = fs::canonical(directory.parent_path());
    } else {
      root = fs::canonical(directory / "service-tools/current");
      mode = "tools";
    }
    const auto path = root / "manifest.json";
    const std::string digest = Hash(path);
    const auto manifest = Manifest(path);
    const auto* python = manifest.Find("python");
    Require(python && python->IsObject(), "missing pinned service interpreter");
    const std::string interpreter = python->GetString("path");
    Require(interpreter.find('\0') == std::string::npos &&
                fs::path(interpreter).is_absolute() &&
                fs::canonical(interpreter) == fs::path(interpreter) &&
                Hash(interpreter) == python->GetString("sha256"),
            "pinned service interpreter changed");
    Require(Hash(path) == digest, "service entry changed");
    std::vector<std::string> arguments = {
        interpreter, "-I",       "-B",
        "-c",        kBootstrap, root.string(),
        digest,      mode,       Hash(executable)};
    for (int i = 2; i < argc; ++i) arguments.emplace_back(argv[i]);
    std::vector<char*> pointers;
    for (auto& argument : arguments) pointers.push_back(argument.data());
    pointers.push_back(nullptr);
    execv(interpreter.c_str(), pointers.data());
    throw std::runtime_error("cannot execute pinned service interpreter");
  } catch (const std::exception&) {
    std::fprintf(stderr,
                 "Service entry unavailable or changed. Keep the verified "
                 "service package or service-tools beside q4t.\n");
    return 1;
  }
}

}  // namespace q4t::service
