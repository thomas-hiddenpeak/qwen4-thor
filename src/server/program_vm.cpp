#include "program_vm.h"

#include <quickjs.h>

#include <chrono>
#include <cmath>
#include <cstring>
#include <string>
#include <unordered_set>
#include <vector>

#include "object_protocol.h"

namespace q4t::server::detail {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
constexpr size_t kMaxCode = 65536;
constexpr size_t kMaxCalls = 64;
constexpr size_t kMaxOutput = 1024 * 1024;
constexpr size_t kMaxArgument = 128 * 1024;
constexpr size_t kMaxJobs = 4096;

struct PromiseSlot {
  JSValue resolve = JS_UNDEFINED;
  JSValue reject = JS_UNDEFINED;
};
struct Execution {
  const Json* input = nullptr;
  JSContext* context = nullptr;
  Clock::time_point deadline = Clock::now() + std::chrono::seconds(1);
  Json calls = Array();
  Json output = Array();
  std::vector<PromiseSlot> promises;
  std::vector<JSValue> rejections;
  size_t bytes = 0;
  size_t jobs = 0;
  std::string fault;
};

void Rejection(JSContext* context, JSValueConst promise, JSValueConst,
               JS_BOOL handled, void* opaque) {
  auto& run = *static_cast<Execution*>(opaque);
  for (auto it = run.rejections.begin(); it != run.rejections.end(); ++it) {
    if (JS_VALUE_GET_PTR(*it) == JS_VALUE_GET_PTR(promise)) {
      if (handled) {
        JS_FreeValue(context, *it);
        run.rejections.erase(it);
      }
      return;
    }
  }
  if (handled) return;
  if (run.rejections.size() >= 64) {
    run.fault = "program_work_limit";
    return;
  }
  try {
    run.rejections.push_back(JS_DupValue(context, promise));
  } catch (...) {
    run.fault = "program_memory_limit";
  }
}

int Interrupt(JSRuntime*, void* opaque) {
  auto& run = *static_cast<Execution*>(opaque);
  if (Clock::now() < run.deadline) return 0;
  run.fault = "program_time_limit";
  return 1;
}

JSValue Fail(JSContext* context, const char* code) {
  auto& run = *static_cast<Execution*>(JS_GetContextOpaque(context));
  // Resource/replay failures cannot be caught to bypass the native contract.
  run.fault = code;
  return JS_ThrowInternalError(context, "%s", code);
}

bool CopyString(JSContext* context, JSValueConst value, std::string* out,
                size_t maximum) {
  size_t size = 0;
  const char* bytes = JS_ToCStringLen(context, &size, value);
  if (!bytes) return false;
  const bool valid = size <= maximum;
  if (valid) out->assign(bytes, size);
  JS_FreeCString(context, bytes);
  return valid;
}

JSValue Tool(JSContext* context, JSValueConst, int argc, JSValueConst* argv,
             int index, JSValue*) {
  try {
    auto& run = *static_cast<Execution*>(JS_GetContextOpaque(context));
    if (!run.fault.empty()) return Fail(context, "program_execution_aborted");
    if (run.calls.array.size() >= kMaxCalls)
      return Fail(context, "program_call_limit");
    const auto& tool = run.input->GetArray("tools")->array.at(index);
    const bool custom = tool.GetString("type") == "custom";
    if (argc != 1 || (custom && !JS_IsString(argv[0])))
      return JS_ThrowTypeError(context, "tool requires one argument");
    JSValue encoded =
        custom ? JS_DupValue(context, argv[0])
               : JS_JSONStringify(context, argv[0], JS_UNDEFINED, JS_UNDEFINED);
    if (JS_IsException(encoded)) return encoded;
    std::string arguments;
    const bool valid = JS_IsString(encoded) &&
                       CopyString(context, encoded, &arguments, kMaxArgument);
    JS_FreeValue(context, encoded);
    if (!valid) return Fail(context, "program_argument_limit");
    const auto ordinal = run.calls.array.size();
    const auto& history = run.input->GetArray("calls")->array;
    if (ordinal < history.size() &&
        (history[ordinal].GetString("key") != tool.GetString("key") ||
         history[ordinal].GetString("arguments") != arguments))
      return Fail(context, "program_replay_mismatch");
    JSValue resolving[2];
    auto promise = JS_NewPromiseCapability(context, resolving);
    if (JS_IsException(promise)) return promise;
    run.promises.push_back({resolving[0], resolving[1]});
    run.calls.array.push_back(
        Object({{"key", *tool.Find("key")}, {"arguments", String(arguments)}}));
    return promise;
  } catch (...) {
    return Fail(context, "program_memory_limit");
  }
}

JSValue Emit(JSContext* context, JSValueConst, int argc, JSValueConst* argv,
             int image, JSValue*) {
  try {
    auto& run = *static_cast<Execution*>(JS_GetContextOpaque(context));
    if (argc != 1 || (image && !JS_IsString(argv[0])))
      return JS_ThrowTypeError(context, "output requires one value");
    JSValue encoded =
        JS_IsString(argv[0]) || JS_IsUndefined(argv[0])
            ? JS_ToString(context, argv[0])
            : JS_JSONStringify(context, argv[0], JS_UNDEFINED, JS_UNDEFINED);
    if (JS_IsException(encoded)) return encoded;
    std::string value;
    const bool valid =
        CopyString(context, encoded, &value, kMaxOutput - run.bytes);
    JS_FreeValue(context, encoded);
    if (!valid || run.output.array.size() >= 256)
      return Fail(context, "program_output_limit");
    run.bytes += value.size();
    run.output.array.push_back(
        Object({{"type", String(image ? "image" : "text")},
                {"value", String(value)}}));
    return JS_UNDEFINED;
  } catch (...) {
    return Fail(context, "program_memory_limit");
  }
}

bool Pump(Execution& run) {
  for (;;) {
    if (!run.fault.empty()) return false;
    if (!JS_IsJobPending(JS_GetRuntime(run.context))) return true;
    if (run.jobs >= kMaxJobs || Clock::now() >= run.deadline) {
      run.fault = "program_work_limit";
      return false;
    }
    JSContext* context = nullptr;
    const int result =
        JS_ExecutePendingJob(JS_GetRuntime(run.context), &context);
    if (result < 0) {
      run.fault = "program_exception";
      return false;
    }
    if (!result) return true;
    ++run.jobs;
  }
}

bool Valid(const Json& input) {
  const auto* code = input.Find("code");
  const auto* tools = input.GetArray("tools");
  const auto* calls = input.GetArray("calls");
  const auto* results = input.GetArray("results");
  if (!input.IsObject() || !code || !code->IsString() ||
      code->str.size() > kMaxCode ||
      code->str.find('\0') != std::string::npos || !tools ||
      tools->array.size() > 128 || !calls || calls->array.size() > kMaxCalls ||
      !results || results->array.size() > calls->array.size())
    return false;
  for (const char* key : {"seed", "epoch_ms"}) {
    const auto* value = input.Find(key);
    if (!value || !value->IsNumber() || !std::isfinite(value->number) ||
        value->number < 0 || std::floor(value->number) != value->number ||
        value->number >
            (std::strcmp(key, "seed") ? 9007199254740991.0 : 4294967295.0))
      return false;
  }
  std::unordered_set<std::string> identities;
  std::unordered_set<std::string> keys;
  for (const auto& tool : tools->array) {
    for (const char* field : {"key", "name", "namespace"}) {
      const auto* value = tool.Find(field);
      if (value &&
          (!value->IsString() || value->str.find('\0') != std::string::npos))
        return false;
    }
    if (!tool.IsObject() || tool.GetString("key").empty() ||
        tool.GetString("key").size() > 256 || tool.GetString("name").empty() ||
        tool.GetString("name").size() > 128 ||
        tool.GetString("namespace").size() > 128 ||
        (tool.GetString("type") != "function" &&
         tool.GetString("type") != "custom") ||
        !keys.insert(tool.GetString("key")).second ||
        !identities
             .insert(tool.GetString("namespace") + '\0' +
                     tool.GetString("name"))
             .second)
      return false;
  }
  for (const auto& call : calls->array)
    if (!call.IsObject() || !keys.contains(call.GetString("key")) ||
        !call.Find("arguments") || !call.Find("arguments")->IsString() ||
        call.GetString("arguments").size() > kMaxArgument)
      return false;
  std::unordered_set<int64_t> completed;
  for (const auto& result : results->array) {
    const auto* index = result.Find("index");
    if (!result.IsObject() || !index || !index->IsNumber() ||
        index->number < 0 || index->number >= calls->array.size() ||
        std::floor(index->number) != index->number || !result.Has("value") ||
        !completed.insert(result.GetInt("index")).second)
      return false;
  }
  return true;
}

bool Install(Execution& run) {
  auto* context = run.context;
  auto global = JS_GetGlobalObject(context);
  auto tools = JS_NewObjectProto(context, JS_NULL);
  bool okay = !JS_IsException(tools);
  const auto& entries = run.input->GetArray("tools")->array;
  std::vector<size_t> order;
  for (int pass = 0; pass < 2; ++pass)
    for (size_t i = 0; i < entries.size(); ++i)
      if (entries[i].GetString("namespace").empty() == (pass == 0))
        order.push_back(i);
  for (const size_t i : order) {
    if (!okay) break;
    const auto& tool = entries[i];
    const auto name_space = tool.GetString("namespace");
    auto owner = JS_DupValue(context, tools);
    if (!name_space.empty()) {
      JS_FreeValue(context, owner);
      owner = JS_GetPropertyStr(context, tools, name_space.c_str());
      if (JS_IsUndefined(owner)) {
        owner = JS_NewObjectProto(context, JS_NULL);
        okay = JS_DefinePropertyValueStr(context, tools, name_space.c_str(),
                                         JS_DupValue(context, owner),
                                         JS_PROP_ENUMERABLE) >= 0;
      } else if (!JS_IsObject(owner)) {
        okay = false;
      }
    }
    if (okay) {
      auto function = JS_NewCFunctionData(context, Tool, 1, i, 0, nullptr);
      okay = JS_DefinePropertyValueStr(context, owner,
                                       tool.GetString("name").c_str(), function,
                                       JS_PROP_ENUMERABLE) >= 0;
    }
    JS_FreeValue(context, owner);
  }
  okay = okay && JS_DefinePropertyValueStr(context, global, "tools", tools,
                                           JS_PROP_ENUMERABLE) >= 0;
  for (int i = 0; okay && i < 2; ++i) {
    auto function = JS_NewCFunctionData(context, Emit, 1, i, 0, nullptr);
    okay = JS_DefinePropertyValueStr(context, global, i ? "image" : "text",
                                     function, JS_PROP_ENUMERABLE) >= 0;
  }
  JS_FreeValue(context, global);
  if (!okay) return false;
  const auto bootstrap = std::string(R"js((() => {
    let seed = )js") + OutputJson(*run.input->Find("seed")) +
                         R"js( >>> 0;
    const random = () => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      return seed / 4294967296;
    };
    Object.defineProperty(Math, 'random', {value: random, writable: false,
                                          configurable: false});
    const original = Date, epoch = )js" +
                         OutputJson(*run.input->Find("epoch_ms")) + R"js(;
    const fixed = new Proxy(original, {
      apply: () => new original(epoch).toString(),
      construct: (target, args, next) =>
        Reflect.construct(target, args.length ? args : [epoch], next)
    });
    Object.defineProperty(original, 'now', {value: () => epoch,
      writable: false, configurable: false});
    Object.defineProperty(original.prototype, 'constructor', {value: fixed,
      writable: false, configurable: false});
    Object.defineProperty(globalThis, 'Date', {value: fixed,
      writable: false, configurable: false});
    for (const value of Object.values(tools))
      if (typeof value === 'object' || typeof value === 'function')
        Object.freeze(value);
    Object.freeze(tools);
  })(); )js";
  auto result = JS_Eval(context, bootstrap.data(), bootstrap.size(),
                        "q4t-program-environment", JS_EVAL_TYPE_GLOBAL);
  okay = !JS_IsException(result);
  JS_FreeValue(context, result);
  return okay;
}
}  // namespace

io::Json EvaluateProgram(const io::Json& input) {
  auto response = Object({{"status", String("incomplete")},
                          {"error", String("program_invalid_state")},
                          {"calls", Array()},
                          {"output", Array()}});
  if (!Valid(input)) return response;
  auto* runtime = JS_NewRuntime();
  if (!runtime) return response;
  JS_SetMemoryLimit(runtime, 32 * 1024 * 1024);
  JS_SetMaxStackSize(runtime, 512 * 1024);
  JS_SetCanBlock(runtime, false);
  auto* context = JS_NewContext(runtime);
  if (!context) {
    JS_FreeRuntime(runtime);
    return response;
  }
  Execution run;
  run.input = &input;
  run.context = context;
  JS_SetContextOpaque(context, &run);
  JS_SetInterruptHandler(runtime, Interrupt, &run);
  JS_SetHostPromiseRejectionTracker(runtime, Rejection, &run);
  JSValue root = JS_UNDEFINED;
  if (!Install(run)) run.fault = "program_environment_failure";
  if (run.fault.empty()) {
    const auto code = input.GetString("code");
    root = JS_Eval(context, code.data(), code.size(), "program.mjs",
                   JS_EVAL_TYPE_MODULE);
    if (JS_IsException(root)) run.fault = "program_exception";
    if (run.fault.empty()) Pump(run);
  }
  // Apply client result events in their recorded order, draining microtasks
  // between them. Replay must preserve Promise.race and dependent call order.
  for (const auto& result : input.GetArray("results")->array) {
    if (!run.fault.empty()) break;
    const auto index = static_cast<size_t>(result.GetInt("index"));
    if (index >= run.promises.size()) {
      run.fault = "program_replay_mismatch";
      break;
    }
    const auto bytes = OutputJson(*result.Find("value"));
    auto value =
        JS_ParseJSON(context, bytes.data(), bytes.size(), "tool-result");
    auto resolved = JS_IsException(value)
                        ? JS_EXCEPTION
                        : JS_Call(context, run.promises[index].resolve,
                                  JS_UNDEFINED, 1, &value);
    JS_FreeValue(context, value);
    if (JS_IsException(resolved)) run.fault = "program_exception";
    JS_FreeValue(context, resolved);
    if (run.fault.empty()) Pump(run);
  }
  if (run.fault.empty() &&
      run.calls.array.size() < input.GetArray("calls")->array.size())
    run.fault = "program_replay_mismatch";
  auto state = JS_PromiseState(context, root);
  if (run.fault.empty() && state == JS_PROMISE_REJECTED)
    run.fault = "program_exception";
  if (run.fault.empty() && !run.rejections.empty())
    run.fault = "program_unhandled_rejection";
  const bool pending =
      run.calls.array.size() > input.GetArray("results")->array.size();
  if (run.fault.empty() && state != JS_PROMISE_FULFILLED && !pending)
    run.fault = "program_unresolved_promise";
  if (run.fault.empty()) {
    Set(&response, "status", String(pending ? "suspended" : "completed"));
    Set(&response, "error", String(""));
    Set(&response, "calls", std::move(run.calls));
    Set(&response, "output", std::move(run.output));
  } else {
    Set(&response, "error", String(run.fault));
  }
  for (const auto& promise : run.promises) {
    JS_FreeValue(context, promise.resolve);
    JS_FreeValue(context, promise.reject);
  }
  JS_SetHostPromiseRejectionTracker(runtime, nullptr, nullptr);
  for (const auto rejection : run.rejections) JS_FreeValue(context, rejection);
  JS_FreeValue(context, root);
  JS_FreeContext(context);
  JS_FreeRuntime(runtime);
  return response;
}
}  // namespace q4t::server::detail
