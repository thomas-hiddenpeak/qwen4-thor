// Shared verified codec loading. Only exec-isolated media workers call Load.
// No library is linked into the core process; audio/video dependencies differ.
#pragma once

#ifdef Q4T_HAVE_GSTREAMER_HEADERS
#include <dlfcn.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstring>
#include <initializer_list>

#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/audio/audio.h>
#include <gst/gst.h>
#include <gst/video/video.h>

#include "object_protocol.h"

namespace q4t::server::media_detail {
using namespace object_detail;
enum class CodecDomain { kVideo, kAudio, kSpeech };
struct Runtime {
#define API(name) decltype(&name) name##_fn = nullptr
  API(gst_init_check);
  API(gst_version);
  API(gst_plugin_register_static);
  API(gst_pipeline_new);
  API(gst_element_factory_make);
  API(gst_bin_add);
  API(gst_element_link);
  API(gst_element_set_state);
  API(gst_element_sync_state_with_parent);
  API(gst_element_query_duration);
  API(gst_element_get_bus);
  API(gst_object_unref);
  API(gst_bus_timed_pop_filtered);
  API(gst_mini_object_unref);
  API(gst_element_get_static_pad);
  API(gst_pad_link);
  API(gst_pad_get_peer);
  API(gst_pad_chain);
  API(gst_pad_add_probe);
  API(gst_pad_get_current_caps);
  API(gst_event_parse_caps);
  API(gst_caps_get_structure);
  API(gst_structure_get_name);
  API(gst_structure_get_int);
  API(gst_structure_has_field);
  API(gst_structure_get_fraction);
  API(gst_caps_from_string);
  API(gst_sample_get_buffer);
  API(gst_sample_get_caps);
  API(gst_buffer_get_size);
  API(gst_buffer_extract);
  API(gst_buffer_new_allocate);
  API(gst_buffer_fill);
  API(gst_sample_get_segment);
  API(gst_segment_to_stream_time);
  API(gst_app_src_set_callbacks);
  API(gst_app_src_push_buffer);
  API(gst_app_src_end_of_stream);
  API(gst_app_sink_try_pull_sample);
  API(gst_app_sink_is_eos);
  API(g_signal_connect_data);
  API(g_object_set);
  API(g_error_free);
  API(gst_audio_info_from_caps);
  API(gst_video_info_from_caps);
  API(gst_video_info_set_format);
  API(gst_video_frame_map);
  API(gst_video_frame_unmap);
#undef API
  Json modules = Array();
  std::vector<int> pinned;
  ~Runtime() {
    for (int fd : pinned) close(fd);
  }
  void* Library(const std::string& path, const char* name, std::string* sha) {
    int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) return nullptr;
    struct stat info {};
    if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != 0 ||
        (info.st_mode & (S_IWGRP | S_IWOTH)) || info.st_size <= 0 ||
        info.st_size > (128LL << 20)) {
      close(fd);
      return nullptr;
    }
    const auto path_fd = "/proc/self/fd/" + std::to_string(fd);
    trace::Digest digest;
    if (!trace::HashFile(path_fd, &digest)) {
      close(fd);
      return nullptr;
    }
    void* handle =
        dlopen(path_fd.c_str(), RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
    if (!handle) {
      close(fd);
      return nullptr;
    }
    pinned.push_back(fd);
    *sha = trace::HexDigest(digest);
    (void)name;
    return handle;
  }
  bool Load(CodecDomain domain = CodecDomain::kVideo) {
    // Fixed C kernels avoid runtime executable mappings in the sandbox.
    setenv("ORC_CODE", "backup", 1);
    std::string core_sha, app_sha, media_sha;
    const std::string root = Q4T_SYSTEM_GSTREAMER_DIRECTORY;
    void* core =
        Library(root + "/libgstreamer-1.0.so.0", "gstreamer", &core_sha);
    void* app = Library(root + "/libgstapp-1.0.so.0", "gstapp", &app_sha);
    const bool audio = domain != CodecDomain::kVideo;
    const char* media_name = audio ? "gstaudio" : "gstvideo";
    void* media = Library(root + "/lib" + media_name + "-1.0.so.0", media_name,
                          &media_sha);
    if (!core || !app || !media) return false;
#define LOAD(handle, name)                                                 \
  name##_fn = reinterpret_cast<decltype(name##_fn)>(dlsym(handle, #name)); \
  if (!name##_fn) return false
    LOAD(core, gst_init_check);
    LOAD(core, gst_version);
    LOAD(core, gst_plugin_register_static);
    LOAD(core, gst_pipeline_new);
    LOAD(core, gst_element_factory_make);
    LOAD(core, gst_bin_add);
    LOAD(core, gst_element_link);
    LOAD(core, gst_element_set_state);
    LOAD(core, gst_element_sync_state_with_parent);
    LOAD(core, gst_element_query_duration);
    LOAD(core, gst_element_get_bus);
    LOAD(core, gst_object_unref);
    LOAD(core, gst_bus_timed_pop_filtered);
    LOAD(core, gst_mini_object_unref);
    LOAD(core, gst_element_get_static_pad);
    LOAD(core, gst_pad_link);
    LOAD(core, gst_pad_get_peer);
    LOAD(core, gst_pad_chain);
    LOAD(core, gst_pad_add_probe);
    LOAD(core, gst_pad_get_current_caps);
    LOAD(core, gst_event_parse_caps);
    LOAD(core, gst_caps_get_structure);
    LOAD(core, gst_structure_get_name);
    LOAD(core, gst_structure_get_int);
    LOAD(core, gst_structure_has_field);
    LOAD(core, gst_structure_get_fraction);
    LOAD(core, gst_caps_from_string);
    LOAD(core, gst_sample_get_buffer);
    LOAD(core, gst_sample_get_caps);
    LOAD(core, gst_buffer_get_size);
    LOAD(core, gst_buffer_extract);
    LOAD(core, gst_buffer_new_allocate);
    LOAD(core, gst_buffer_fill);
    LOAD(core, gst_sample_get_segment);
    LOAD(core, gst_segment_to_stream_time);
    LOAD(core, g_signal_connect_data);
    LOAD(core, g_object_set);
    LOAD(core, g_error_free);
    LOAD(app, gst_app_src_set_callbacks);
    LOAD(app, gst_app_src_push_buffer);
    LOAD(app, gst_app_src_end_of_stream);
    LOAD(app, gst_app_sink_try_pull_sample);
    LOAD(app, gst_app_sink_is_eos);
    if (audio) {
      LOAD(media, gst_audio_info_from_caps);
    } else {
      LOAD(media, gst_video_info_from_caps);
      LOAD(media, gst_video_info_set_format);
      LOAD(media, gst_video_frame_map);
      LOAD(media, gst_video_frame_unmap);
    }
#undef LOAD
    // No registry scan, plugin-scanner subprocess, user path or autoplugging.
    setenv("GST_PLUGIN_SYSTEM_PATH_1_0", "", 1);
    setenv("GST_PLUGIN_PATH_1_0", "", 1);
    setenv("GST_REGISTRY", "/q4t-no-video-registry", 1);
    setenv("GST_REGISTRY_UPDATE", "no", 1);
    setenv("GST_REGISTRY_FORK", "no", 1);
    GError* error = nullptr;
    const bool initialized = gst_init_check_fn(nullptr, nullptr, &error);
    if (error) g_error_free_fn(error);
    if (!initialized) return false;
    guint major, minor, micro, nano;
    gst_version_fn(&major, &minor, &micro, &nano);
    if (major != 1 || minor < 20) return false;
    const auto version = std::to_string(major) + '.' + std::to_string(minor) +
                         '.' + std::to_string(micro);
    modules.array.push_back(Object({{"name", String("gstreamer")},
                                    {"version", String(version)},
                                    {"sha256", String(core_sha)}}));
    modules.array.push_back(Object({{"name", String("gstapp")},
                                    {"version", String(version)},
                                    {"sha256", String(app_sha)}}));
    modules.array.push_back(Object({{"name", String(media_name)},
                                    {"version", String(version)},
                                    {"sha256", String(media_sha)}}));
    const std::initializer_list<const char*> video_plugins = {
        "coreelements",    "app",      "isomp4", "matroska",
        "videoparsersbad", "openh264", "vpx",    "videoconvertscale",
        "videofilter",     "png"};
    const std::initializer_list<const char*> audio_plugins = {
        "coreelements", "app",     "audioconvert", "audioresample", "wavparse",
        "audioparsers", "flac",    "ogg",          "opus",          "vorbis",
        "mpg123",       "isomp4",  "matroska",     "faad",          "alaw",
        "mulaw",        "adpcmdec"};
    const std::initializer_list<const char*> speech_plugins = {
        "coreelements", "app", "flac", "ogg", "opus", "lame", "voaacenc"};
    const auto plugins = domain == CodecDomain::kSpeech ? speech_plugins
                         : audio ? audio_plugins : video_plugins;
    for (const char* name : plugins) {
      std::string sha;
      auto handle =
          Library(root + "/gstreamer-1.0/libgst" + name + ".so", name, &sha);
      if (!handle)
        continue;  // A missing format does not disable other formats.
      const auto symbol = std::string("gst_plugin_") + name + "_get_desc";
      const auto describe = reinterpret_cast<const GstPluginDesc* (*)()>(
          dlsym(handle, symbol.c_str()));
      if (!describe) continue;
      const auto* desc = describe();
      if (!desc || !desc->name || !desc->version ||
          std::strlen(desc->version) > 64 ||
          !gst_plugin_register_static_fn(
              desc->major_version, desc->minor_version, desc->name,
              desc->description, desc->plugin_init, desc->version,
              desc->license, desc->source, desc->package, desc->origin))
        continue;
      modules.array.push_back(Object({{"name", String(name)},
                                      {"version", String(desc->version)},
                                      {"sha256", String(sha)}}));
    }
    // All /proc/self/fd names remained distinct until the last dlopen.
    for (int fd : pinned) close(fd);
    pinned.clear();
    return true;
  }
  void Mini(void* object) {
    if (object)
      gst_mini_object_unref_fn(reinterpret_cast<GstMiniObject*>(object));
  }
};
}  // namespace q4t::server::media_detail
#endif
