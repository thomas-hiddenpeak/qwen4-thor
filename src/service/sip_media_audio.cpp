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
// An idle telephone-event source has no natural EOS. The voice payloader's
// serialized EOS follows all queued audio, including the resampler tail.
GstPadProbeReturn VoiceEos(GstPad*, GstPadProbeInfo* info, gpointer output) {
  if (GST_EVENT_TYPE(GST_PAD_PROBE_INFO_EVENT(info)) != GST_EVENT_EOS)
    return GST_PAD_PROBE_OK;
  gst_pad_push_event(GST_PAD(output), gst_event_new_eos());
  return GST_PAD_PROBE_DROP;
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
  gst_clear_object(&tones_);
  gst_clear_object(&bus_);
  gst_clear_object(&pipe_);
}
bool SipMediaAudio::Prepare(const LiveRtpCodec& c, const SipDtmfFormat& dtmf,
                            bool transmit, int wake_fd) {
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
  const bool events = dtmf.payload >= 0;
  const bool split = events && dtmf.rate != c.rate;
  std::string output = " ! session.send_rtp_sink ";
  std::string rtp_output = "session.send_rtp_src ! ";
  std::string rtcp_output = "session.send_rtcp_src ! ";
  std::string rtcp_input = " ! session.recv_rtcp_sink";
  if (events) {
    const std::string generator =
        transmit ? "rtpdtmfsrc name=tones pt=" + std::to_string(dtmf.payload) +
                       " clock-rate=" + std::to_string(dtmf.rate) +
                       " packet-redundancy=3 ! "
                 : "";
    if (split) {
      // Different RTP clocks require separate sender SSRCs and sender reports.
      output +=
          "rtpsession name=keys rtcp-min-interval=1000000000 " +
          (transmit ? generator + "keys.send_rtp_sink " : "") + source +
          "event_in "
          "caps=application/x-rtp,media=audio,encoding-name=TELEPHONE-EVENT,"
          "payload=(int)" +
          std::to_string(dtmf.payload) + ",clock-rate=(int)" +
          std::to_string(dtmf.rate) +
          " ! keys.recv_rtp_sink keys.recv_rtp_src ! fakesink sync=false "
          "async=false ";
      if (transmit)
        rtp_output =
            "keys.send_rtp_src ! tones_mux.sink_1 session.send_rtp_src ! "
            "tones_mux.sink_0 funnel name=tones_mux ! ";
      rtcp_output =
          "keys.send_rtcp_src ! reports.sink_1 session.send_rtcp_src ! "
          "reports.sink_0 funnel name=reports ! ";
      rtcp_input =
          " ! tee name=feedback feedback. ! session.recv_rtcp_sink "
          "feedback. ! keys.recv_rtcp_sink";
    } else if (transmit) {
      // Preserve simultaneous speech and keys in one RTP sequence space.
      output =
          " ! tones_mux.sink_0 rtpmux name=tones_mux ! session.send_rtp_sink " +
          generator + "tones_mux.sink_1 ";
    }
  }
  // Element names come only from the compiled codec table.
  const std::string graph =
      "rtpsession name=session rtcp-min-interval=1000000000 " + source +
      "pcm_in " + (dtmf.payload >= 0 ? "max-latency=250000000 " : "") +
      "caps=" + raw +
      " ! audioconvert ! audioresample ! audio/x-raw,rate=" + rate + " ! " +
      c.encoder + " ! " + c.pay + " pt=" + payload + " max-ptime=20000000 ! " +
      rtp + output + rtp_output + sink + "rtp_out " + rtcp_output + sink +
      "rtcp_out " + source + "rtp_in caps=" + rtp +
      " ! session.recv_rtp_sink " +
      "session.recv_rtp_src ! rtpjitterbuffer name=voice_jitter latency=60 "
      "drop-on-latency=true "
      "! " +
      rtp + " ! " + c.depay + " ! " + c.decoder +
      " ! audioconvert ! audioresample ! " + raw + " ! " + sink + "pcm_out " +
      source + "rtcp_in caps=application/x-rtcp" + rtcp_input;
  g_autoptr(GError) error = nullptr;
  pipe_ = gst_parse_launch(graph.c_str(), &error);
  if (!pipe_ || error || !GST_IS_PIPELINE(pipe_)) return false;
  tones_ = gst_bin_get_by_name(GST_BIN(pipe_), "tones");
  if (tones_) {
    g_autoptr(GstElement) mux =
        gst_bin_get_by_name(GST_BIN(pipe_), "tones_mux");
    g_autoptr(GstPad) voice = gst_element_get_static_pad(mux, "sink_0");
    auto* out = gst_element_get_static_pad(mux, "src");
    if (!voice || !out) {
      gst_clear_object(&out);
      return false;
    }
    gst_pad_add_probe(voice, GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM, VoiceEos, out,
                      gst_object_unref);
  }
  if (split) {
    sources_[kEvent] = gst_bin_get_by_name(GST_BIN(pipe_), "event_in");
    g_autoptr(GstElement) voice =
        gst_bin_get_by_name(GST_BIN(pipe_), "session");
    g_autoptr(GstElement) keys = gst_bin_get_by_name(GST_BIN(pipe_), "keys");
    GstStructure* sdes = nullptr;
    g_object_get(voice, "sdes", &sdes, nullptr);
    if (sdes) {
      g_object_set(keys, "sdes", sdes, nullptr);
      gst_structure_free(sdes);
    }
  } else if (events) {
    // Count authenticated event packets in the shared RTP session, then
    // remove them before the speech jitter buffer and decoder.
    g_autoptr(GstElement) jitter =
        gst_bin_get_by_name(GST_BIN(pipe_), "voice_jitter");
    g_autoptr(GstPad) pad = gst_element_get_static_pad(jitter, "sink");
    gst_pad_add_probe(
        pad, GST_PAD_PROBE_TYPE_BUFFER,
        [](GstPad*, GstPadProbeInfo* info, gpointer pt) {
          unsigned char header[2];
          auto* buffer = GST_PAD_PROBE_INFO_BUFFER(info);
          if (gst_buffer_extract(buffer, 0, header, 2) != 2 ||
              (header[1] & 127) == GPOINTER_TO_INT(pt))
            return GST_PAD_PROBE_DROP;
          return GST_PAD_PROBE_OK;
        },
        GINT_TO_POINTER(dtmf.payload), nullptr);
  }
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
bool SipMediaAudio::Tone(unsigned number, bool start) {
  if (!tones_) return false;
  auto* shape = gst_structure_new(
      "dtmf-event", "type", G_TYPE_INT, 1, "method", G_TYPE_INT, 1, "start",
      G_TYPE_BOOLEAN, start, "number", G_TYPE_INT, static_cast<int>(number),
      "volume", G_TYPE_INT, 10, nullptr);
  g_autoptr(GstPad) pad = gst_element_get_static_pad(tones_, "src");
  return gst_pad_send_event(
      pad, gst_event_new_custom(GST_EVENT_CUSTOM_UPSTREAM, shape));
}
bool SipMediaAudio::Start() {
  return gst_element_set_state(pipe_, GST_STATE_PLAYING) !=
         GST_STATE_CHANGE_FAILURE;
}
bool SipMediaAudio::Push(int lane, std::span<const unsigned char> data) {
  if (lane == kEvent && !sources_[lane]) lane = kRtp;
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
  for (auto* source : sources_)
    if (source) gst_app_src_end_of_stream(GST_APP_SRC(source));
}
bool SipMediaAudio::OutputEnded() const {
  return gst_app_sink_is_eos(GST_APP_SINK(sinks_[kRtp]));
}
}  // namespace q4t::service
