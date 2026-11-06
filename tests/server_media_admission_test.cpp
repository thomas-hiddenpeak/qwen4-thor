#include "q4t/server/media_admission.h"

#include <array>
#include <limits>

#include "q4t/server/chat_capabilities.h"
#include "q4t/server/chat_contract.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using namespace q4t::server;
constexpr const char* kPng =
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPYoiECAAKE"
    "APFkHeRLAAAAAElFTkSuQmCC";
constexpr const char* kHeader272 =
    "iVBORw0KGgoAAAANSUhEUgAAARAAAAEQCAIAAAD3aLxC";

bool Contract(const std::string& part, const std::string& role = "user",
              std::string* param = nullptr) {
  Json request;
  if (!q4t::io::ParseJson("{\"messages\":[{\"role\":\"" + role +
                              "\",\"content\":[" + part + "]}]}",
                          &request, true))
    return false;
  return ValidateChatContract(request, "m", true, param).ok();
}
struct Fixture {
  std::vector<VisionItem> items;
  std::vector<int32_t> tokens = {9, 1, 8};
  q4t::vision::ProcessorConfig image, video;
  q4t::vision::MediaDeviceBudget device{48128};
  ChatPlanConfig config;
  q4t::vision::MediaPlan plan;
  ChatPlanError error;
  Fixture() {
    std::string bytes;
    const char* code = nullptr;
    DecodeMediaUrl(kHeader272, &bytes, &code);
    items.push_back({VisionItem::kImage, {bytes}, 4, 3});
    config.model = "m";
    config.max_prefill = 8192;
    video.min_pixels = 4096;
    video.max_pixels = 25165824;
  }
  q4t::Status Plan() {
    return PlanChatMedia(items, tokens, 1, 2, image, video, device, config,
                         &plan, &error);
  }
};
}  // namespace

Q4T_TEST(media_contract_closed_shapes) {
  Q4T_CHECK(Contract(
      R"({"type":"image_url","image_url":{"url":"x","detail":"auto"}})"));
  Q4T_CHECK(Contract(R"({"image_url":"x"})"));
  Q4T_CHECK(
      Contract(R"({"type":"video_frames","video_frames":["x",{"url":"y"}]})"));
  for (const char* part :
       {R"({"image_url":{"url":"x","unknown":1}})",
        R"({"image_url":{"url":"x","detail":"low"}})",
        R"({"image_url":{"url":"x","detail":"high"}})",
        R"({"image_url":{"url":2}})", R"({"image_url":null})",
        R"({"image_url":""})", R"({"image_url":"x","text":"x"})",
        R"({"image_url":"x","video_frames":["x","x"]})",
        R"({"type":"text","image_url":"x"})",
        R"({"type":"video_url","video_url":"x"})", R"({"video_frames":["x"]})",
        R"({"video_frames":[]})",
        R"({"video_frames":["x",{"url":"y","extra":1}]})"}) {
    Q4T_CHECK(!Contract(part));
  }
  return true;
}

Q4T_TEST(media_contract_roles_and_locations) {
  for (const char* role : {"system", "developer", "assistant", "tool"})
    Q4T_CHECK(!Contract(R"({"image_url":"x"})", role));
  std::string param;
  Q4T_CHECK(
      !Contract(R"({"video_frames":["x",{"url":false}]})", "user", &param));
  Q4T_CHECK(param == "messages[0].content[0].video_frames[1]");
  Q4T_CHECK(!Contract(R"({"image_url":{"url":"x","detail":"high"}})", "user",
                      &param));
  Q4T_CHECK(param == "messages[0].content[0].image_url");
  return true;
}

Q4T_TEST(media_contract_item_frame_limits) {
  std::string frames = R"({"video_frames":["x")";
  for (int i = 1; i < 32; ++i) frames += ",\"x\"";
  Q4T_CHECK(Contract(frames + "]}"));
  Q4T_CHECK(!Contract(frames + ",\"x\"]}"));
  std::string items = R"({"image_url":"x"})";
  for (int i = 1; i < 8; ++i) items += R"(,{"image_url":"x"})";
  Q4T_CHECK(Contract(items));
  Q4T_CHECK(!Contract(items + R"(,{"image_url":"x"})"));
  return true;
}

Q4T_TEST(media_transport_base64_and_mime) {
  std::string bytes;
  const char* code = nullptr;
  Q4T_CHECK(DecodeMediaUrl(kPng, &bytes, &code));
  const auto expected = bytes;
  Q4T_CHECK(DecodeMediaUrl(std::string("data:image/png;base64,") + kPng, &bytes,
                           &code));
  Q4T_CHECK(bytes == expected);
  Q4T_CHECK(!DecodeMediaUrl(std::string("data:image/jpeg;base64,") + kPng,
                            &bytes, &code));
  Q4T_CHECK(std::string(code) == "media_format_mismatch" && bytes.empty());
  for (const std::string input :
       {"", "A", "AA", "AAA", "AA=", "A===", "=AAA", "AA=A",
        "AB==", "AAB=", "____", "----", "AA==\n", "AA ==", "AAAA="}) {
    Q4T_CHECK(!DecodeMediaUrl(input, &bytes, &code));
    Q4T_CHECK(std::string(code) == "media_base64_invalid" && bytes.empty());
  }
  for (const char* input : {"data:image/gif;base64,AAAA", "http://a/b.png",
                            "https://a/b.jpg", "data:image/png,AAAA"}) {
    Q4T_CHECK(!DecodeMediaUrl(input, &bytes, &code));
    Q4T_CHECK(std::string(code) == "media_url_unsupported");
  }
  Q4T_CHECK(!DecodeMediaUrl("AA==", &bytes, &code));
  Q4T_CHECK(std::string(code) == "media_format_unsupported");
  return true;
}

Q4T_TEST(media_prompt_transport_error_location) {
  Json request;
  Q4T_CHECK(q4t::io::ParseJson(R"({"messages":[{"role":"user","content":[
    {"text":"x"},{"video_frames":["AAAA","AA=A"]}]}]})",
                               &request, true));
  std::string prompt, param, code;
  std::vector<VisionItem> items;
  Q4T_CHECK(!PrepareChatPrompt(request, {}, &prompt, &items, &param, &code));
  Q4T_CHECK(param == "messages[0].content[1].video_frames[0]");
  Q4T_CHECK(code == "media_format_unsupported");
  return true;
}

Q4T_TEST(media_prompt_valid_template_order) {
  Json request;
  const std::string data = "data:image/png;base64," + std::string(kPng);
  Q4T_CHECK(
      q4t::io::ParseJson("{\"messages\":[{\"role\":\"user\",\"content\":["
                         "{\"text\":\"look\"},{\"image_url\":\"" +
                             data +
                             "\"},"
                             "{\"video_frames\":[\"" +
                             data + "\",\"" + data + "\"]}]}]}",
                         &request, true));
  std::string prompt, param, code;
  std::vector<VisionItem> items;
  Q4T_CHECK(PrepareChatPrompt(request, {}, &prompt, &items, &param, &code));
  Q4T_CHECK(items.size() == 2 && items[0].kind == VisionItem::kImage &&
            items[1].kind == VisionItem::kVideo);
  Q4T_CHECK(items[0].part_index == 1 && items[1].part_index == 2);
  Q4T_CHECK(prompt.find("<|image_pad|>") < prompt.find("<|video_pad|>"));
  return true;
}

Q4T_TEST(media_plan_expanded_slot_budget) {
  Fixture f;
  f.tokens.assign(100, 9);
  f.tokens[1] = 1;
  Q4T_CHECK(f.Plan());
  Q4T_CHECK(f.plan.merged_tokens == 64 && f.plan.expanded_prompt_tokens == 163);
  f.config.max_seq = 2;
  f.config.capacities = {256, 512};
  Json request;
  Q4T_CHECK(
      q4t::io::ParseJson(R"({"messages":[],"max_tokens":128})", &request));
  ChatPromptBudget budget;
  Q4T_CHECK(PlanChatBudget(request, f.config, f.plan.expanded_prompt_tokens,
                           &budget, &f.error));
  Q4T_CHECK(budget.required_capacity == 291 &&
            budget.eligible_slots == std::vector<int>{1});
  f.config.max_prefill = 162;
  Q4T_CHECK(!f.Plan());
  Q4T_CHECK(f.error.code == "media_prefill_limit" &&
            f.error.param == "messages");
  f.config.max_prefill = 8192;
  f.config.max_len = 163;
  Q4T_CHECK(!f.Plan());
  Q4T_CHECK(f.error.code == "context_length_exceeded");
  return true;
}

Q4T_TEST(media_plan_placeholder_order) {
  Fixture f;
  for (const std::vector<int32_t>& tokens :
       {std::vector<int32_t>{9}, {9, 2}, {1, 1}}) {
    f.tokens = tokens;
    Q4T_CHECK(!f.Plan());
    Q4T_CHECK(f.error.code == "media_placeholder_count" &&
              f.plan.item_count == 0);
  }
  const auto bytes = f.items[0].frames[0];
  f.items.push_back({VisionItem::kVideo, {bytes, bytes}, 5, 2});
  f.tokens = {2, 1};
  Q4T_CHECK(!f.Plan());
  f.tokens = {1, 2};
  Q4T_CHECK(f.Plan());
  return true;
}

Q4T_TEST(media_plan_tail_error_and_device_limit) {
  Fixture f;
  const auto bytes = f.items[0].frames[0];
  f.items[0] = {VisionItem::kVideo, std::vector<std::string>(32, bytes), 4, 3};
  f.items[0].frames.back() = "invalid";
  f.tokens.assign(16, 2);
  Q4T_CHECK(!f.Plan());
  Q4T_CHECK(f.error.code == "media_format_unsupported" &&
            f.error.param == "messages[4].content[3].video_frames[31]");
  f.items[0].frames.back() = bytes;
  f.device.attention_shared_bytes = 0;
  Q4T_CHECK(!f.Plan());
  Q4T_CHECK(f.error.code == "media_device_limit_unknown");
  f.device.attention_shared_bytes = 1;
  Q4T_CHECK(!f.Plan());
  Q4T_CHECK(f.error.code == "media_attention_limit");
  return true;
}

Q4T_TEST(media_policy_discovery) {
  Json policy, caps, config;
  Q4T_CHECK(q4t::io::ParseJson(MediaLimitsJson(48128), &policy, true));
  Q4T_CHECK(policy.GetInt("attention_shared_bytes") == 48128);
  Q4T_CHECK(policy.GetInt("decoder_owned_bytes") == 268435456);
  Q4T_CHECK(policy.GetInt("max_items") == 8);
  Q4T_CHECK(!policy.GetBool("whole_server_ram_limit", true));
  Q4T_CHECK(q4t::io::ParseJson(ChatCapabilitiesJson("m", true, false, 48128),
                               &caps, true));
  Q4T_CHECK(caps.Find("media")->GetInt("attention_shared_bytes") == 48128);
  ChatPlanConfig effective;
  effective.model = "m";
  effective.allow_media = true;
  effective.media_attention_shared_bytes = 48128;
  Q4T_CHECK(q4t::io::ParseJson(
      EffectiveServeConfigJson({}, effective, true, false, 128), &config,
      true));
  Q4T_CHECK(config.Find("runtime")
                ->Find("media_limits")
                ->GetInt("attention_shared_bytes") == 48128);
  Q4T_CHECK(q4t::io::ParseJson(MediaLimitsJson(0), &policy, true));
  Q4T_CHECK(policy.Find("attention_shared_bytes")->IsNull());
  return true;
}

Q4T_TEST(media_video_timestamps_and_parameter_paths) {
  Q4T_CHECK(Contract(
      R"({"video_frames":["x","y","z"],"video_timestamps":[0,0.5,2]})"));
  for (const char* times : {"null", "[]", "[0]", "[0,1,2]", "[0,0]",
                            "[1,0]", "[-1,0]", "[0,86401]", "[0,true]",
                            R"([0,"1"])", "[0,{}]"}) {
    std::string param;
    Q4T_CHECK(!Contract(std::string(R"({"video_frames":["x","y"],)"
                                     R"("video_timestamps":)") + times + "}",
                         "user", &param));
    Q4T_CHECK(param.starts_with("messages[0].content[0].video_timestamps"));
  }
  Q4T_CHECK(!Contract(R"({"image_url":"x","video_timestamps":[0]})"));
  Json part;
  Q4T_CHECK(q4t::io::ParseJson("{}", &part));
  std::vector<double> times;
  std::string field;
  Q4T_CHECK(VideoFrameTimes(part, 3, &times, &field));
  Q4T_CHECK(times == (std::vector<double>{0, 1.0 / 24, 2.0 / 24}));
  return true;
}

Q4T_TEST(media_video_template_splits_temporal_groups) {
  const std::string frames = std::string("\"") + kPng + "\"";
  Json request;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"enable_thinking":false,"messages":[{"role":"user","content":[)"
      R"({"text":"before"},{"video_frames":[)" + frames + "," + frames +
          "," + frames + R"(],"video_timestamps":[0,1,3]},{"text":"after"}]}]})",
      &request, true));
  std::string prompt, param, code;
  std::vector<VisionItem> items;
  Q4T_CHECK(PrepareChatPrompt(request, {}, &prompt, &items, &param, &code));
  Q4T_CHECK(items.size() == 1 && items[0].frames.size() == 3);
  Q4T_CHECK(prompt.find(
      "before<0.5 seconds><|vision_start|><|video_pad|><|vision_end|>"
      "<3.0 seconds><|vision_start|><|video_pad|><|vision_end|>after") !=
            std::string::npos);
  return true;
}

Q4T_TEST(media_expansion_and_client_budget_are_identical) {
  std::string bytes;
  const char* code;
  Q4T_CHECK(DecodeMediaUrl(kPng, &bytes, &code));
  const std::vector<VisionItem> items{
      {VisionItem::kImage, {bytes}, 0, 0},
      {VisionItem::kVideo, {bytes, bytes, bytes, bytes}, 0, 1}};
  const std::vector<uint32_t> unexpanded{9, 1, 8, 2, 7, 2, 6};
  std::vector<uint32_t> tokens = unexpanded;
  q4t::vision::ProcessorConfig image, video;
  video.min_pixels = 4096;
  video.max_pixels = 25165824;
  const q4t::vision::MediaDeviceBudget device{48128};
  ChatPlanConfig config;
  config.max_prefill = 2048;
  config.max_len = 8192;
  Json request;
  Q4T_CHECK(q4t::io::ParseJson(R"({"messages":[],"max_tokens":32})", &request));
  q4t::vision::MediaPlan plan;
  ChatPromptBudget budget;
  ChatPlanError error;
  Q4T_CHECK(ExpandChatMedia(request, items, 1, 2, image, video, device, config,
                            &tokens, &plan, &budget, &error));
  std::vector<uint32_t> expected{9};
  expected.insert(expected.end(), 64, 1);
  expected.push_back(8);
  expected.insert(expected.end(), 1, 2);
  expected.push_back(7);
  expected.insert(expected.end(), 1, 2);
  expected.push_back(6);
  Q4T_CHECK(tokens == expected && tokens.size() == 70);
  Q4T_CHECK(plan.merged_tokens == 66 && plan.expanded_prompt_tokens == 70);
  ClientLimits limits;
  limits.max_prompt_tokens = 69;
  limits.max_output_tokens = 32;
  config.client_limits = &limits;
  tokens = unexpanded;
  Q4T_CHECK(!ExpandChatMedia(request, items, 1, 2, image, video, device, config,
                             &tokens, &plan, &budget, &error));
  Q4T_CHECK(error.code == "client_prompt_limit_exceeded");
  Q4T_CHECK(tokens == unexpanded && plan.item_count == 0);
  limits.max_prompt_tokens = 70;
  Q4T_CHECK(ExpandChatMedia(request, items, 1, 2, image, video, device, config,
                            &tokens, &plan, &budget, &error));
  Q4T_CHECK(tokens == expected && budget.available_output == 32);
  return true;
}

Q4T_TEST(media_constraints_preserve_input_and_force_output_contract) {
  for (const std::string controls : {
           R"("response_format":{"type":"json_object"})",
           R"("tool_choice":"none")",
           R"("tool_choice":"required","tools":[{"type":"function",)"
           R"("function":{"name":"inspect","strict":true,"parameters":)"
           R"({"type":"object","properties":{},"additionalProperties":false}}}])"}) {
    Json request;
    Q4T_CHECK(q4t::io::ParseJson(
        R"({"messages":[{"role":"user","content":[{"image_url":")" +
            std::string(kPng) + R"("},{"text":"inspect"}]}],)" + controls + "}",
        &request, true));
    std::string param;
    Q4T_CHECK(ValidateChatContract(request, "m", true, &param));
    Q4T_CHECK(!ValidateChatContract(request, "m", false, &param));
    OutputConstraints constraints;
    Q4T_CHECK(PlanOutputConstraints(request, &constraints, &param));
    Q4T_CHECK(constraints.active);
    std::string prompt, code;
    std::vector<VisionItem> items;
    Q4T_CHECK(PrepareChatPrompt(request, constraints, &prompt, &items, &param,
                                &code));
    Q4T_CHECK(items.size() == 1 && items[0].kind == VisionItem::kImage);
    Q4T_CHECK(prompt.find("<|image_pad|>") != std::string::npos);
  }
  return true;
}
