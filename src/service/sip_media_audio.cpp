#include "sip_media_audio.h"

#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <sys/eventfd.h>

#include <string>

namespace q4t::service {
namespace {
GstFlowReturn Wake(GstAppSink*, gpointer user) {
  eventfd_write(GPOINTER_TO_INT(user), 1);
  return GST_FLOW_OK;
}
}  // namespace
SipMediaAudio::~SipMediaAudio() {
  if (pipe_) {
    // A live RTP session has three source branches. Release streaming waits
    // before joining its RTCP thread during transition to NULL.
    gst_element_send_event(pipe_, gst_event_new_flush_start());
    gst_element_set_state(pipe_, GST_STATE_NULL);
  }
  for (auto*& e : sources_) gst_clear_object(&e);
  for (auto*& e : sinks_) gst_clear_object(&e);
  gst_clear_object(&bus_);
  gst_clear_object(&pipe_);
}
bool SipMediaAudio::Prepare(const LiveRtpCodec& c, int wake_fd) {
  const auto rate = std::to_string(c.rate), payload = std::to_string(c.payload);
  const std::string rtp =
      "application/x-rtp,media=audio,encoding-name=" + std::string(c.name) +
      ",payload=(int)" + payload + ",clock-rate=(int)" + rate;
  const std::string raw =
      "audio/x-raw,format=S16LE,layout=interleaved,channels=1,rate=24000";
  const std::string sink =
      "appsink sync=false async=false wait-on-eos=false max-buffers=16 "
      "emit-signals=true name=";
  const std::string source =
      "appsrc is-live=true format=time do-timestamp=true max-bytes=131072 "
      "name=";
  // Element names come only from the compiled codec table.
  const std::string graph =
      "rtpsession name=session rtcp-min-interval=1000000000 " + source +
      "pcm_in caps=" + raw +
      " ! audioconvert ! audioresample ! audio/x-raw,rate=" + rate + " ! " +
      c.encoder + " ! " + c.pay + " pt=" + payload + " max-ptime=20000000 ! " +
      rtp + " ! session.send_rtp_sink " + "session.send_rtp_src ! " + sink +
      "rtp_out session.send_rtcp_src ! " + sink + "rtcp_out " + source +
      "rtp_in caps=" + rtp + " ! session.recv_rtp_sink " +
      "session.recv_rtp_src ! rtpjitterbuffer latency=60 drop-on-latency=true "
      "! " +
      rtp + " ! " + c.depay + " ! " + c.decoder +
      " ! audioconvert ! audioresample ! " + raw + " ! " + sink + "pcm_out " +
      source + "rtcp_in caps=application/x-rtcp ! session.recv_rtcp_sink";
  g_autoptr(GError) error = nullptr;
  pipe_ = gst_parse_launch(graph.c_str(), &error);
  if (!pipe_ || error || !GST_IS_PIPELINE(pipe_)) return false;
  bus_ = gst_element_get_bus(pipe_);
  const char* inputs[] = {"pcm_in", "rtp_in", "rtcp_in"};
  const char* outputs[] = {"pcm_out", "rtp_out", "rtcp_out"};
  for (int i = 0; i < 3; ++i) {
    sources_[i] = gst_bin_get_by_name(GST_BIN(pipe_), inputs[i]);
    sinks_[i] = gst_bin_get_by_name(GST_BIN(pipe_), outputs[i]);
    if (!sources_[i] || !sinks_[i]) return false;
    g_signal_connect(sinks_[i], "new-sample", G_CALLBACK(Wake),
                     GINT_TO_POINTER(wake_fd));
  }
  return gst_element_set_state(pipe_, GST_STATE_READY) !=
         GST_STATE_CHANGE_FAILURE;
}
bool SipMediaAudio::Start() {
  return gst_element_set_state(pipe_, GST_STATE_PLAYING) !=
         GST_STATE_CHANGE_FAILURE;
}
bool SipMediaAudio::Push(int lane, std::span<const unsigned char> data) {
  auto* src = GST_APP_SRC(sources_[lane]);
  if (data.empty() || data.size() > 65535 ||
      gst_app_src_get_current_level_bytes(src) > 131072 - data.size())
    return false;
  auto* buf = gst_buffer_new_allocate(nullptr, data.size(), nullptr);
  if (!buf) return false;
  gst_buffer_fill(buf, 0, data.data(), data.size());
  if (lane == kPcm)
    GST_BUFFER_DURATION(buf) =
        gst_util_uint64_scale(data.size(), GST_SECOND, 48000);
  return gst_app_src_push_buffer(src, buf) == GST_FLOW_OK;
}
GstSample* SipMediaAudio::Pull(int lane) {
  return gst_app_sink_try_pull_sample(GST_APP_SINK(sinks_[lane]), 0);
}
bool SipMediaAudio::Healthy() const {
  g_autoptr(GstMessage) error = gst_bus_pop_filtered(bus_, GST_MESSAGE_ERROR);
  return !error;
}
void SipMediaAudio::EndAudio() {
  for (auto* source : sources_) gst_app_src_end_of_stream(GST_APP_SRC(source));
}
bool SipMediaAudio::OutputEnded() const {
  return gst_app_sink_is_eos(GST_APP_SINK(sinks_[kRtp]));
}
}  // namespace q4t::service
