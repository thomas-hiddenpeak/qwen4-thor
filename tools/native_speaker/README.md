# Native speaker backend

The native ECAPA-TDNN backend provides sequential speaker labels for
`POST /v1/audio/transcriptions`, including request-local known-speaker
references, `diarized_json` and SSE. It combines actual speaker embeddings,
VAD chunks, ASR text and forced-alignment word times. Unknown labels follow
first appearance. It does not yet separate simultaneous voices; actual
overlap detection/separation remains a required implementation task.

## Fixed assets and setup

Use `speechbrain/spkrec-ecapa-voxceleb` at model revision
`0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`. The source
`embedding_model.ckpt` has SHA256
`0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2`.
Keep model assets outside the source tree or under `.q4t-work/`; do not modify
the project's read-only main-model directory. The fixed upstream source,
authors and Apache-2.0 license are recorded in the
[license notice](../../third_party/licenses/speechbrain/README.md).

Conversion is an offline development operation using CPU PyTorch. It preserves
every original tensor value and requires a new output directory:

```bash
CUDA_VISIBLE_DEVICES='' /path/to/cpu-python -B \
  tools/native_speaker/convert_weights.py \
  --source /absolute/path/to/embedding_model.ckpt \
  --output .q4t-work/speaker-assets
cp tools/native_speaker/model-config.json .q4t-work/speaker-assets/config.json
```

The converter emits `model.safetensors` and `conversion.json`; copying the
canonical config is a separate step. The accepted weight SHA256 is
`901bb7278ba2f4f43076854e1e2757d7ae8c15f429b16b42e35cd729c75754af`;
the config SHA256 is
`a173ae14a11bacabd30a71535bc581e5aab1a5871855ba939e002b3284a398ac`.
Serving reads the native fixed assets and uses existing CUDA/cuBLASLt;
PyTorch, SpeechBrain, pickle loading and remote model services are not runtime
dependencies. Weights are not included in the repository.

Enable `--speaker-model-dir /absolute/path/to/speaker-assets` together with
`--asr-model-dir`, `--aligner-model-dir` and `--vad-model-dir`. Managed service
prepare accepts the same options and binds identities and limits into recovery
qualification. It cannot silently drop the speaker backend during fallback.

| Option | CLI default | Accepted rev296 managed setting |
|---|---:|---:|
| `--speaker-window-ms` | 3000 | 3000 |
| `--speaker-step-ms` | 750 | 750 |
| `--speaker-max-windows` | 256 | 8 |
| `--speaker-max-speakers` | 16 | 16 |
| `--speaker-workspace-mib` | 128 | 128 |

These limits bound actual work and return explicit capacity errors. Workspace,
weights and staging all count toward Thor's shared physical RAM. Ordinary
asset/backend faults degrade the speaker capability independently; a healthy
core stays available. Actual shared-device failures retain device protection.

## Client use

With the official Python client configured for this deployment:

```python
with open("conversation.wav", "rb") as audio:
    result = client.audio.transcriptions.create(
        model="your-served-model-name",
        file=audio,
        response_format="diarized_json",
        chunking_strategy="auto",
    )
for segment in result.segments:
    print(segment.speaker, segment.start, segment.end, segment.text)
```

`stream=True` returns linked text delta, completed segment and final events.
Time boundaries come from the aligner; this is not word-at-a-time ASR streaming.
For known speakers, provide matching `known_speaker_names` and
`known_speaker_references` arrays: at most four names and 2–10 second audio
data-URL references. They are decoded and matched within the request only.
No persistent identity lookup is performed.

## Validation and delivery

- [Native numeric comparison](../../docs/evidence/serve-speaker-compute-20261118/README.md):
  complete features and seven ECAPA stages against fixed upstream CPU code.
- [Actual HTTP and SDK](../../docs/evidence/serve-speaker-http-20261118/README.md):
  same voice, A/B/A recurrence, known references, JSON/SSE, silence, cancellation,
  capacity and request-driven recovery while the core keeps its PID.
- [Primary/light profiles](../../docs/evidence/serve-speaker-profiles-20261118/README.md)
  and [native TLS/service recovery](../../docs/evidence/serve-speaker-service-20261118/README.md):
  real diarized business before and after recovery.
- [Reversible rev296 delivery](../../docs/evidence/serve-speaker-delivery-20261118/README.md):
  stopped publication, actual whole-root rollback and forward restoration.

The retained controls use the current qualified engine; this is not evidence
for an independently qualified older engine. Original failed attempts are
preserved. These bounded checks do not establish general diarization accuracy,
overlap handling, full-model quality, performance or long-term stability.
The remaining audio and master-plan work continues after this delivery.
