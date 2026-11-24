#include "eval_run_state.h"

#include <algorithm>
#include <ctime>

#include "eval_grading.h"
#include "eval_model_grading.h"
#include "eval_sampling.h"
#include "q4t/server/binary_store.h"
#include "q4t/server/request_json.h"
#include "q4t/server/schema_graph.h"

namespace q4t::server::eval_detail {
namespace {
using namespace object_detail;

ObjectReply FileRow(ObjectStore& objects, const StateAccess& access,
                    ObjectRequest* write, Json* row, bool* done) {
  BinaryAccess authorization{access.scope, access.deadline, access.cancelled};
  BinaryRead read;
  BinaryStore binary(objects);
  auto opened =
      binary.Open(authorization, ObjectKind::kEvalRun, write->id, 60, &read);
  if (!opened.ok()) return opened;
  // Open pins the exact root with a conditional write as part of its receipt.
  // A concurrent cancel or step changes the revision and makes our CAS fail.
  ++write->expected_revision;
  auto next = [&]() -> ObjectReply {
    auto index = write->data.GetInt("chunk", -1),
         offset = write->data.GetInt("offset", -1);
    if (index < 0 || offset < 0 ||
        static_cast<size_t>(index) > read.manifest.chunks.size())
      return InvalidRecord();
    std::string line;
    bool newline = false;
    while (!newline &&
           static_cast<size_t>(index) < read.manifest.chunks.size()) {
      std::string bytes;
      auto got = binary.Read(authorization, read, index, &bytes);
      if (!got.ok()) return got;
      if (static_cast<size_t>(offset) >= bytes.size()) return InvalidRecord();
      const auto end = bytes.find('\n', offset);
      const size_t span = (end == bytes.npos ? bytes.size() : end) - offset;
      if (span > kChatBodyBytes - line.size())
        return Error(413, "eval_row_too_large",
                     "JSONL row exceeds application body capacity");
      line.append(bytes, offset, span);
      newline = end != bytes.npos;
      offset += span + (newline ? 1 : 0);
      if (static_cast<size_t>(offset) == bytes.size()) {
        ++index;
        offset = 0;
      }
    }
    Set(&write->data, "chunk", Number(index));
    Set(&write->data, "offset", Number(offset));
    if (!newline && line.empty()) {
      *done = true;
      return {};
    }
    auto valid = io::ParseJson(line, row, true, kApplicationJsonLimits);
    return valid ? ObjectReply{}
                 : BadInput("data_source.source", valid.message());
  };
  auto result = next();
  auto closed = binary.Close(authorization, &read);
  if (!closed.ok()) objects.RecordAuxiliaryCleanupFailure();
  return result;
}

Json Sample(const Json& row) {
  const auto* stored = row.Find("sample");
  Json sample = Object({{"input", Array()},
                        {"output", Array()},
                        {"finish_reason", String("")},
                        {"model", String("")},
                        {"temperature", Number(0)},
                        {"top_p", Number(0)},
                        {"max_completion_tokens", Number(0)},
                        {"seed", Number(0)},
                        {"error", Json()},
                        {"usage", Object({{"total_tokens", Number(0)},
                                          {"completion_tokens", Number(0)},
                                          {"prompt_tokens", Number(0)},
                                          {"cached_tokens", Number(0)}})}});
  if (!stored) return sample;
  for (auto& [field, value] : sample.object) {
    const auto* from = stored->Find(field);
    if (from && field != "usage") value = *from;
    if (from && field == "usage" && from->IsObject()) {
      for (auto& [key, count] : value.object)
        if (const auto* actual = from->Find(key)) count = *actual;
    }
  }
  if (!stored->Has("output") && stored->Find("output_text") &&
      stored->Find("output_text")->IsString())
    Set(&sample, "output",
        Array({Object({{"role", String("assistant")},
                       {"content", *stored->Find("output_text")}})}));
  return sample;
}

ObjectReply ValidateRow(const Json& schema, const Json& row) {
  const auto* item = row.Find("item");
  const auto* sample = row.Find("sample");
  if (!row.IsObject() || !item || !item->IsObject() ||
      (sample && !sample->IsObject()))
    return BadInput(
        "data_source.source",
        "Dataset row requires an item object and optional sample object");
  const auto* props = schema.Find("properties");
  const auto* item_schema = props ? props->Find("item") : nullptr;
  if (!item_schema) return InvalidRecord();
  OutputSchemaGraph graph;
  std::string param;
  auto valid = graph.Init(*item_schema, false, &param, "item",
                          OutputSchemaPurpose::kToolResult);
  if (!valid) return InvalidRecord();
  valid = graph.Check(*item);
  return valid ? ObjectReply{}
               : BadInput("data_source.source.item", valid.message());
}

ObjectReply Finish(ObjectStore& objects, const StateAccess& access,
                   ObjectRequest& write, Json resource,
                   const ObjectReply& error) {
  Set(&resource, "status", String(error.ok() ? "completed" : "failed"));
  if (!error.ok())
    Set(&resource, "error",
        Object({{"code", String(error.code)},
                {"message", String(error.message)}}));
  Set(&write.data, "resource", std::move(resource));
  return objects.Execute(write, access.deadline, access.cancelled);
}

ObjectReply Score(const Json& criteria, const Json& row,
                  const StateAccess& access, const Json* model_results,
                  Json* results, Json* totals, bool* passed) {
  size_t budget = kChatBodyBytes;
  size_t comparison_work = 8 * 1024 * 1024;
  if (!criteria.IsArray() || !totals->IsArray() ||
      totals->array.size() != criteria.array.size())
    return InvalidRecord();
  for (size_t i = 0; i < criteria.array.size(); ++i) {
    const auto& grader = criteria.array[i];
    const auto type = grader.GetString("type");
    ObjectReply result;
    if (ModelCriterion(grader)) {
      const auto* grade =
          model_results ? model_results->Find(std::to_string(i)) : nullptr;
      if (!grade || !grade->IsObject()) return InvalidRecord();
      result.value = *grade;
    } else if (type == "string_check")
      result = GradeString(grader, row, &budget);
    else if (type == "python")
      result = GradePython(grader, row, access);
    else if (type == "text_similarity")
      result = GradeSimilarity(grader, row, &budget, &comparison_work);
    else
      return BadInput("testing_criteria",
                      "Grader requires a separate execution provider");
    if (!result.ok()) return result;
    const bool matched = result.value.GetBool("passed");
    const auto field = matched ? "passed" : "failed";
    Set(&totals->array[i], field, Number(totals->array[i].GetInt(field) + 1));
    *passed &= matched;
    results->array.push_back(std::move(result.value));
  }
  return {};
}
}  // namespace

ObjectReply AdvanceRun(ObjectStore& objects, const StateAccess& access,
                       const Json& record, SamplingOwner* owner) {
  using namespace object_detail;
  auto write = Update(ObjectKind::kEvalRun, access, record);
  if (write.data.GetBool("deleting"))
    return ResumeRunDeletion(objects, access, record,
                             owner ? &owner->jobs : nullptr);
  const auto* original = write.data.Find("resource");
  if (!original) return InvalidRecord();
  auto resource = *original;
  const auto status = resource.GetString("status");
  if (status != "queued" && status != "in_progress")
    return ReleaseSampling(objects, access, owner ? &owner->jobs : nullptr, write);
  auto& totals = Field(resource, "per_testing_criteria_results");
  const auto* criteria = write.data.GetArray("criteria");
  const auto* data_source = resource.Find("data_source");
  if (!criteria || !data_source) return InvalidRecord();
  // A frozen rate snapshot belongs to the retiring service generation. Allow
  // cancellation/deletion above, but leave model work for the next owner.
  if (owner && owner->clients.RatesFrozen() &&
      (data_source->GetString("type") != "jsonl" ||
       std::any_of(criteria->array.begin(), criteria->array.end(), ModelCriterion))) {
    ObjectReply waiting;
    waiting.http_status = 202;
    return waiting;
  }
  if (status == "queued") {
    totals = Array();
    for (const auto& grader : criteria->array)
      totals.array.push_back(
          Object({{"testing_criteria", String(grader.GetString("name"))},
                  {"passed", Number(0)},
                  {"failed", Number(0)}}));
    Set(&resource, "status", String("in_progress"));
    Set(&write.data, "resource", std::move(resource));
    return objects.Execute(write, access.deadline, access.cancelled);
  }
  const auto* source = data_source->Find("source");
  const auto index = write.data.GetInt("row", -1);
  if (!source || index < 0) return InvalidRecord();
  Json row;
  bool done = false;
  ObjectReply loaded;
  const auto source_type = source->GetString("type");
  const auto* grading_row = write.data.Find("grading_row");
  if (grading_row) {
    row = *grading_row;
  } else if (write.data.Has("pending_input")) {
    const auto* saved = write.data.Find("pending_input");
    if (!saved) return InvalidRecord();
    row = *saved;
  } else if (source_type == "file_content") {
    const auto* content = source->GetArray("content");
    if (!content || static_cast<size_t>(index) > content->array.size())
      return InvalidRecord();
    done = static_cast<size_t>(index) == content->array.size();
    if (!done) row = content->array[index];
  } else if (source_type == "file_id" || write.data.Has("blob")) {
    loaded = FileRow(objects, access, &write, &row, &done);
  } else {
    loaded = BadInput("data_source.source",
                      "Dataset requires a stored inference source");
  }
  if (loaded.http_status >= 500 || loaded.http_status == 499) return loaded;
  if (!loaded.ok() || done)
    return Finish(objects, access, write, resource, loaded);
  const auto* schema = write.data.Find("schema");
  if (!schema) return InvalidRecord();
  auto checked = ValidateRow(*schema, row);
  if (!checked.ok()) return Finish(objects, access, write, resource, checked);
  ObjectRequest consumed;
  if (!grading_row && data_source->GetString("type") != "jsonl") {
    if (!owner)
      return Error(503, "eval_sample_unavailable",
                   "Model sampling owner is unavailable");
    auto sampled =
        SampleRow(objects, access, *owner, write, *data_source, &row, &consumed);
    if (!sampled.ok()) {
      if (sampled.http_status >= 500 || sampled.http_status == 409 ||
          sampled.http_status == 429 || sampled.http_status == 499 ||
          sampled.commit_unknown)
        return sampled;
      return Finish(objects, access, write, resource, sampled);
    }
    if (sampled.http_status == 202) return sampled;
    const auto& sample = *row.Find("sample");
    Set(&resource, "model", *sample.Find("model"));
    RecordModelUsage(&resource, sample);
  }
  auto graded = AdvanceModelGrade(objects, access, owner, write, resource, row,
                                  &consumed);
  if (graded.http_status == 413 && !graded.commit_unknown) {
    auto previous = Update(ObjectKind::kEvalRun, access, record);
    previous.expected_revision = write.expected_revision;
    return Finish(objects, access, previous, *previous.data.Find("resource"),
                  graded);
  }
  if (!graded.ok() || graded.http_status == 202) return graded;
  auto results = Array();
  bool passed = true;
  auto scored =
      Score(*write.data.GetArray("criteria"), row, access,
            write.data.Find("model_results"), &results, &totals, &passed);
  auto sample = Sample(row);
  if (!scored.ok())
    Set(&sample, "error",
        Object({{"code", String(scored.code)},
                {"message", String(scored.message)}}));
  const auto id = NewObjectId(ObjectKind::kEvalOutput);
  if (id.empty())
    return Error(503, "eval_id_unavailable", "Cannot allocate output ID");
  auto output = Read(ObjectKind::kEvalOutput, access, id);
  output.operation = ObjectOperation::kPut;
  output.ttl_seconds = 0;
  output.created_at = std::time(nullptr);
  auto item = Object({{"object", String("eval.run.output_item")},
                      {"id", String(id)},
                      {"run_id", String(write.id)},
                      {"eval_id", *resource.Find("eval_id")},
                      {"created_at", Number(output.created_at)},
                      {"datasource_item_id", Number(index)},
                      {"datasource_item", *row.Find("item")},
                      {"sample", std::move(sample)},
                      {"results", std::move(results)},
                      {"status", String(!scored.ok() ? "error"
                                        : passed     ? "pass"
                                                     : "fail")}});
  if (!Shape(item, "output_item").ok())
    return Finish(
        objects, access, write, resource,
        BadInput("sample",
                 "Dataset sample does not match the output contract"));
  output.data = Object({{"format", String("q4t.eval_output.v1")},
                        {"resource", std::move(item)}});
  auto& counts = Field(resource, "result_counts");
  const auto field = !scored.ok() ? "errored" : passed ? "passed" : "failed";
  Set(&counts, field, Number(counts.GetInt(field) + 1));
  Set(&counts, "total", Number(index + 1));
  Set(&write.data, "resource", std::move(resource));
  Set(&write.data, "row", Number(index + 1));
  Field(write.data, "output_ids").array.push_back(String(id));
  ClearModelGrades(write.data);
  if (!consumed.id.empty()) ClearSampling(write.data);
  std::vector<ObjectRequest> writes{std::move(write), std::move(output)};
  if (!consumed.id.empty()) writes.push_back(std::move(consumed));
  auto committed =
      objects.ExecuteBatch(writes, access.deadline, access.cancelled);
  if (committed.http_status == 413 && !committed.commit_unknown) {
    // A rejected completion transaction published neither the item nor its
    // counters. End this run at the last committed row instead of retrying it.
    auto previous = Update(ObjectKind::kEvalRun, access, record);
    previous.expected_revision = writes.front().expected_revision;
    const auto prior_resource = *previous.data.Find("resource");
    return Finish(objects, access, previous, prior_resource, committed);
  }
  return committed;
}
}  // namespace q4t::server::eval_detail
