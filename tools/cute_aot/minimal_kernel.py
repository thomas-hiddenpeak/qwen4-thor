"""Step 1a: minimal CuTe DSL kernel -> AOT .h/.o -> (C++ dlopen later)."""
import os
import sys

import cutlass
import cutlass.cute as cute
from cutlass.cute.runtime import make_ptr
from cutlass import Int32


class VecAdd:
    """Minimal elementwise add: out[i] = x[i] + y[i]."""

    @cute.kernel
    def kernel(self, x: cute.Pointer, y: cute.Pointer, out: cute.Pointer,
               n: Int32):
        tidx, _, _ = cute.arch.thread_idx()
        bidx, _, _ = cute.arch.block_idx()
        for i in range(bidx * 256 + tidx, n, 256):
            out[i] = x[i] + y[i]

    @cute.jit
    def __call__(self, x: cute.Pointer, y: cute.Pointer, out: cute.Pointer,
                 n: Int32):
        self.kernel(x, y, out, n).launch(
            grid=(Int32(1024), 1, 1),
            block=[256, 1, 1],
        )


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out_dir, exist_ok=True)

    dummy = 0
    x = make_ptr(cutlass.Float32, dummy, assumed_align=16)
    y = make_ptr(cutlass.Float32, dummy, assumed_align=16)
    out = make_ptr(cutlass.Float32, dummy, assumed_align=16)
    n = Int32(0)

    compiled = cute.compile(VecAdd(), x, y, out, n,
                            options="--gpu-arch sm_110a")
    compiled.export_to_c(out_dir, "vec_add")
    print("exported:", os.path.join(out_dir, "vec_add.h"),
          os.path.join(out_dir, "vec_add.o"))


if __name__ == "__main__":
    main()
