"""Extract exact tested kernels; generated inputs stay in .q4t-work/."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".q4t-work/vision-attention-rowmax-20261107/numerical-input"


def extract(source):
    helpers = source[source.index("__device__ __forceinline__ float Bf16ToFloat"):
                     source.index("// LayerNorm (with affine")]
    kernel = source[source.index("__global__ void AttentionKernel("):
                    source.index("// Host: build 2D RoPE")]
    return helpers, kernel


def main():
    source = "src/vision/vision.cu"
    parent = subprocess.check_output(["git", "show", "f7dcd8f9:" + source], cwd=ROOT).decode()
    candidate = (ROOT / source).read_text()
    tested = subprocess.check_output(["git", "show", "c6eef3c2:" + source], cwd=ROOT).decode()
    if candidate != tested:
        raise RuntimeError("runtime changed since successful HTTP pilot")
    old_helpers, old = extract(parent)
    helpers, new = extract(candidate)
    if helpers != old_helpers:
        raise RuntimeError("conversion helpers changed")
    OUT.mkdir()
    content = "constexpr int kBlock = 256;\n" + helpers
    content += old.replace("void AttentionKernel(", "void ParentAttentionKernel(", 1)
    content += new.replace("void AttentionKernel(", "void CandidateAttentionKernel(", 1)
    (OUT / "kernels.inc").write_text(content)
    (OUT / "manifest.json").write_text(json.dumps(dict(
        parent_source_sha256=hashlib.sha256(parent.encode()).hexdigest(),
        candidate_source_sha256=hashlib.sha256(candidate.encode()).hexdigest(),
        generated_sha256=hashlib.sha256(content.encode()).hexdigest(),
        transform="Exact helper and kernel bodies; only kernel entry names differ.",
        planned_shapes=[1, 2, 3, 31, 32, 33, 63, 64, 65, 127, 128, 129,
                        255, 256, 257, 384, 1024, 2640, 8192],
        extra_cases="t=4,S=384; six S=257 boundary inputs; 26 cases total",
        contract="All output BF16 bits identical; no tolerance or timing claim."), indent=2) + "\n")


if __name__ == "__main__":
    main()
