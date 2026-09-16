// Standalone correctness validation of a chunked gated-delta-rule kernel vs the
// sequential golden. One block = one head. Correctness-first: all GEMMs run on
// warp 0. C=32, dk=dv=128. Validates the tensor-core chunked approach + layouts.
//
// VALIDATED 2026-09-16: y l2_rel=3.05e-3, S l2_rel=2.42e-3 vs the sequential
// golden (T=70, 5 chunks incl. a partial last chunk) — bf16 GEMM precision,
// matching the existing kernel's ~6e-3 vs the fp32 reference. Proves the full
// pipeline: 6 tensor-core GEMMs (A=KKᵀ, QKᵀ, K@S0, Q@S0, P@delta, deltaᵀ@KD),
// the transposed S_T[dv,dk] / delta_T[dv,C] layouts (so mma B-operands need no
// transpose), forward substitution, decay scaling, and chunk state-carry.
//
// Build/run: nvcc -arch=sm_110a -o gdn_chunk gdn_chunk_proto.cu && ./gdn_chunk
// (dims here are reduced via #defines to fit 48KB static shared; see main()).
//
// INTEGRATION TODO (to land in linear_attention.cu behind a flag):
//   1. Real dims dk=dv=128: buffers total ~282KB > 228KB. Use dynamic shared +
//      buffer aliasing, OR vd-split (each block owns dv/2 or dv/4 columns; the
//      dv columns are independent) to shrink S_T/S_Tb/Sdel/KS0/QS0 and raise
//      occupancy above 1 block/SM.
//   2. Multi-warp GEMMs: WarpGemm here runs on warp 0 only (3/4 warps idle).
//      Tile M/N across the 4 warps for throughput.
//   3. Gating math: this proto takes precomputed alpha/beta; the engine kernel
//      must fold in softplus(a+dt_bias), exp(A_log), sigmoid(beta), L2-norm of
//      k/q (matching GatedDeltaNetKernel exactly).
//   4. Validate vs GatedDeltaNetKernel (golden) + measure real prefill.
#include <cstdio>
#include <cstdint>
#include <vector>
#include <random>
#include <cmath>
#include <cuda_runtime.h>
#include <cuda_bf16.h>

using u16 = uint16_t;
#define CH 16
#define DK 32
#define DV 32

__device__ __forceinline__ float b2f(u16 h) {
  uint32_t u = (uint32_t)h << 16; float f; memcpy(&f, &u, 4); return f;
}
__device__ __forceinline__ u16 f2b(float f) {
  __nv_bfloat16 b = __float2bfloat16_rn(f); return *reinterpret_cast<u16*>(&b);
}
__device__ __forceinline__ void Mma(float& c0, float& c1, float& c2, float& c3,
                                    uint32_t a0, uint32_t a1, uint32_t a2,
                                    uint32_t a3, uint32_t b0, uint32_t b1) {
  asm volatile(
      "mma.sync.aligned.m16n8k16.row.col.f32.bf16.bf16.f32 "
      "{%0,%1,%2,%3}, {%4,%5,%6,%7}, {%8,%9}, {%0,%1,%2,%3};\n"
      : "+f"(c0), "+f"(c1), "+f"(c2), "+f"(c3)
      : "r"(a0), "r"(a1), "r"(a2), "r"(a3), "r"(b0), "r"(b1));
}
// Cout[M,N] f32 = A[M,K] @ B[K,N]; A row-major [M,K], Bt row-major [N,K]
// (Bt[n][k]=B[k][n]). Executed by ONE warp (lanes 0..31). K%16==0.
__device__ void WarpGemm(float* Cout, const u16* A, const u16* Bt, int M, int N,
                         int K) {
  const int lane = threadIdx.x & 31;
  const int group = lane >> 2, k0 = (lane & 3) * 2, col = (lane & 3) * 2;
  for (int mt = 0; mt < (M + 15) / 16; ++mt)
    for (int nt = 0; nt < (N + 7) / 8; ++nt) {
      float c0 = 0, c1 = 0, c2 = 0, c3 = 0;
      const int mb = mt * 16, nb = nt * 8;
      for (int kt = 0; kt < K / 16; ++kt) {
        const int kb = kt * 16;
        auto ld = [&](const u16* base, int r, int kk) -> uint32_t {
          return *reinterpret_cast<const uint32_t*>(base + r * K + kb + kk);
        };
        uint32_t a0 = ld(A, mb + group, k0), a1 = ld(A, mb + group + 8, k0);
        uint32_t a2 = ld(A, mb + group, k0 + 8), a3 = ld(A, mb + group + 8, k0 + 8);
        uint32_t b0 = ld(Bt, nb + group, k0), b1 = ld(Bt, nb + group, k0 + 8);
        Mma(c0, c1, c2, c3, a0, a1, a2, a3, b0, b1);
      }
      if (mb + group < M) {
        if (nb + col < N) Cout[(mb + group) * N + nb + col] = c0;
        if (nb + col + 1 < N) Cout[(mb + group) * N + nb + col + 1] = c1;
      }
      if (mb + group + 8 < M) {
        if (nb + col < N) Cout[(mb + group + 8) * N + nb + col] = c2;
        if (nb + col + 1 < N) Cout[(mb + group + 8) * N + nb + col + 1] = c3;
      }
    }
}

__global__ void ChunkKernel(const u16* k_in, const u16* q_in, const u16* v_in,
                            const float* alpha_in, const float* beta_in,
                            float* ssm, u16* y_out, int T) {
  const int tid = threadIdx.x, j = tid;  // 128 threads, thread j owns dv-col j
  __shared__ float S_T[DV * DK];         // state^T [dv,dk] f32 (persistent)
  __shared__ u16 S_Tb[DV * DK];          // state^T bf16 (GEMM B-operand)
  __shared__ u16 Kc[CH * DK], Qc[CH * DK];
  __shared__ float Vc[CH * DV];
  __shared__ float A[CH * CH], QK[CH * CH];
  __shared__ float KS0[CH * DV], QS0[CH * DV];
  __shared__ float deltaT[DV * CH];      // delta^T [dv,C] f32
  __shared__ u16 deltaTb[DV * CH];       // delta^T bf16
  __shared__ u16 Pb[CH * CH];            // P [C,C] bf16
  __shared__ u16 KTs[DK * CH];           // KD^T [dk,C] bf16
  __shared__ float Ydelta[CH * DV], Sdel[DV * DK];
  __shared__ float Gsh[CH], betaSh[CH];

  for (int e = tid; e < DK * DV; e += blockDim.x) {  // S_T[j,i]=ssm[i,j]
    int jj = e / DK, i = e % DK;
    S_T[e] = ssm[i * DV + jj];
  }
  __syncthreads();

  for (int c0 = 0; c0 < T; c0 += CH) {
    const int n = min(CH, T - c0);
    for (int e = tid; e < CH * DK; e += blockDim.x) {
      int r = e / DK, i = e % DK;
      Kc[e] = (r < n) ? k_in[(c0 + r) * DK + i] : 0;
      Qc[e] = (r < n) ? q_in[(c0 + r) * DK + i] : 0;
    }
    for (int e = tid; e < CH * DV; e += blockDim.x) {
      int r = e / DV, c = e % DV;
      Vc[e] = (r < n) ? b2f(v_in[(c0 + r) * DV + c]) : 0.f;
    }
    for (int e = tid; e < DK * DV; e += blockDim.x) S_Tb[e] = f2b(S_T[e]);
    if (tid < CH) {
      float g = 0.f;
      for (int r = 0; r <= tid && r < n; ++r) g += logf(alpha_in[c0 + r]);
      Gsh[tid] = g;
      betaSh[tid] = (tid < n) ? beta_in[c0 + tid] : 0.f;
    }
    __syncthreads();

    if ((tid >> 5) == 0) {
      WarpGemm(A, Kc, Kc, CH, CH, DK);            // A_raw = Kc Kc^T
      WarpGemm(QK, Qc, Kc, CH, CH, DK);           // QK_raw = Qc Kc^T
      WarpGemm(KS0, Kc, S_Tb, CH, DV, DK);        // Kc @ S0
      WarpGemm(QS0, Qc, S_Tb, CH, DV, DK);        // Qc @ S0
    }
    __syncthreads();
    for (int e = tid; e < CH * CH; e += blockDim.x) {    // scale A (strict lower)
      int r = e / CH, p = e % CH;
      A[e] = (p < r && r < n) ? A[e] * expf(Gsh[r] - Gsh[p]) * betaSh[p] : 0.f;
    }
    __syncthreads();
    // forward substitution: delta_T[j,r] = rhs - sum_{p<r} A[r,p] delta_T[j,p]
    for (int r = 0; r < n; ++r) {
      float rhs = Vc[r * DV + j] - expf(Gsh[r]) * KS0[r * DV + j];
      float acc = 0.f;
      for (int p = 0; p < r; ++p) acc += A[r * CH + p] * deltaT[j * CH + p];
      deltaT[j * CH + r] = rhs - acc;
    }
    for (int r = n; r < CH; ++r) deltaT[j * CH + r] = 0.f;
    for (int r = 0; r < CH; ++r) deltaTb[j * CH + r] = f2b(deltaT[j * CH + r]);
    // P[t,r] = QK[t,r]*exp(G_t-G_r)*beta_r  (r<=t)
    for (int e = tid; e < CH * CH; e += blockDim.x) {
      int t = e / CH, r = e % CH;
      float p = (r <= t && t < n) ? QK[e] * expf(Gsh[t] - Gsh[r]) * betaSh[r] : 0.f;
      Pb[e] = f2b(p);
    }
    // KTs[i,r] = exp(G_last-G_r)*beta_r*Kc[r,i]  (KD^T [dk,C])
    const float Glast = Gsh[n - 1];
    for (int e = tid; e < DK * CH; e += blockDim.x) {
      int i = e / CH, r = e % CH;
      float s = (r < n) ? expf(Glast - Gsh[r]) * betaSh[r] * b2f(Kc[r * DK + i]) : 0.f;
      KTs[e] = f2b(s);
    }
    __syncthreads();
    if ((tid >> 5) == 0) {
      WarpGemm(Ydelta, Pb, deltaTb, CH, DV, CH);  // P @ delta
      WarpGemm(Sdel, deltaTb, KTs, DV, DK, CH);   // delta^T @ KD -> Sdelta^T[dv,dk]
    }
    __syncthreads();
    for (int r = 0; r < n; ++r)
      y_out[(c0 + r) * DV + j] =
          f2b(expf(Gsh[r]) * QS0[r * DV + j] + Ydelta[r * DV + j]);
    for (int i = 0; i < DK; ++i)
      S_T[j * DK + i] = expf(Glast) * S_T[j * DK + i] + Sdel[j * DK + i];
    __syncthreads();
  }
  for (int e = tid; e < DK * DV; e += blockDim.x) {  // ssm[i,j] = S_T[j,i]
    int jj = e / DK, i = e % DK;
    ssm[i * DV + jj] = S_T[e];
  }
}

static u16 hf2b(float f){__nv_bfloat16 b=__float2bfloat16_rn(f);return *reinterpret_cast<u16*>(&b);}
static float hb2f(u16 h){uint32_t u=(uint32_t)h<<16;float f;memcpy(&f,&u,4);return f;}

int main() {
  const int T = 70;
  std::mt19937 rng(3);
  std::normal_distribution<float> d(0.f, 1.f);
  std::uniform_real_distribution<float> ua(0.85f, 0.999f), ub(0.1f, 0.9f);
  std::vector<float> kf(T*DK), qf(T*DK), vf(T*DV), al(T), be(T), S0(DK*DV);
  for (int t=0;t<T;++t){
    float nk=0,nq=0;
    for(int i=0;i<DK;++i){kf[t*DK+i]=d(rng);qf[t*DK+i]=d(rng);nk+=kf[t*DK+i]*kf[t*DK+i];nq+=qf[t*DK+i]*qf[t*DK+i];}
    nk=1.f/std::sqrt(nk);nq=1.f/std::sqrt(nq)/std::sqrt((float)DK);
    for(int i=0;i<DK;++i){kf[t*DK+i]*=nk;qf[t*DK+i]*=nq;}
    for(int c=0;c<DV;++c)vf[t*DV+c]=d(rng);
    al[t]=ua(rng);be[t]=ub(rng);
  }
  for(auto&x:S0)x=d(rng)*0.1f;
  std::vector<u16> kh(T*DK),qh(T*DK),vh(T*DV);
  for(int i=0;i<T*DK;++i){kh[i]=hf2b(kf[i]);qh[i]=hf2b(qf[i]);}
  for(int i=0;i<T*DV;++i)vh[i]=hf2b(vf[i]);
  std::vector<float> Sg(S0), yg(T*DV);
  for(int t=0;t<T;++t){
    std::vector<float> kS(DV,0);
    for(int c=0;c<DV;++c){float a=0;for(int i=0;i<DK;++i)a+=hb2f(kh[t*DK+i])*Sg[i*DV+c];kS[c]=a;}
    std::vector<float> del(DV);
    for(int c=0;c<DV;++c)del[c]=hb2f(vh[t*DV+c])-al[t]*kS[c];
    for(int i=0;i<DK;++i)for(int c=0;c<DV;++c)Sg[i*DV+c]=al[t]*Sg[i*DV+c]+be[t]*hb2f(kh[t*DK+i])*del[c];
    for(int c=0;c<DV;++c){float a=0;for(int i=0;i<DK;++i)a+=hb2f(qh[t*DK+i])*Sg[i*DV+c];yg[t*DV+c]=a;}
  }
  u16 *dk,*dq,*dv; float *dal,*dbe,*dssm; u16* dy;
  cudaMalloc(&dk,T*DK*2);cudaMalloc(&dq,T*DK*2);cudaMalloc(&dv,T*DV*2);
  cudaMalloc(&dal,T*4);cudaMalloc(&dbe,T*4);cudaMalloc(&dssm,DK*DV*4);cudaMalloc(&dy,T*DV*2);
  cudaMemcpy(dk,kh.data(),T*DK*2,cudaMemcpyHostToDevice);
  cudaMemcpy(dq,qh.data(),T*DK*2,cudaMemcpyHostToDevice);
  cudaMemcpy(dv,vh.data(),T*DV*2,cudaMemcpyHostToDevice);
  cudaMemcpy(dal,al.data(),T*4,cudaMemcpyHostToDevice);
  cudaMemcpy(dbe,be.data(),T*4,cudaMemcpyHostToDevice);
  cudaMemcpy(dssm,S0.data(),DK*DV*4,cudaMemcpyHostToDevice);
  cudaFuncSetAttribute(ChunkKernel, cudaFuncAttributeMaxDynamicSharedMemorySize, 0);
  ChunkKernel<<<1,32>>>(dk,dq,dv,dal,dbe,dssm,dy,T);
  cudaError_t err=cudaDeviceSynchronize();
  if(err!=cudaSuccess){printf("CUDA err: %s\n",cudaGetErrorString(err));return 1;}
  std::vector<u16> yh(T*DV); std::vector<float> Sh(DK*DV);
  cudaMemcpy(yh.data(),dy,T*DV*2,cudaMemcpyDeviceToHost);
  cudaMemcpy(Sh.data(),dssm,DK*DV*4,cudaMemcpyDeviceToHost);
  double yn=0,yd=0,sn=0,sd=0;
  for(int i=0;i<T*DV;++i){double diff=hb2f(yh[i])-yg[i];yn+=diff*diff;yd+=yg[i]*yg[i];}
  for(int i=0;i<DK*DV;++i){double diff=Sh[i]-Sg[i];sn+=diff*diff;sd+=Sg[i]*Sg[i];}
  printf("y l2_rel=%.4e  S l2_rel=%.4e  (T=%d)\n",std::sqrt(yn/yd),std::sqrt(sn/sd),T);
  return 0;
}
