"""Validate the chunked gated-delta-rule decomposition against the sequential
golden recurrence (the exact form implemented in linear_attention.cu).

Sequential per token t:
  kS    = k_t^T S           # [dv]
  delta = v_t - alpha_t*kS
  S     = alpha_t*S + beta_t * outer(k_t, delta)
  y_t   = q_t^T S

Chunked (derived): within a chunk, with g_t=log(alpha_t), cumulative
G_t = sum_{s<=t} g_s (inclusive), S0 = state before chunk:
  rhs_r = v_r - exp(G_r) * (k_r^T S0)
  A[r,p] = exp(G_r - G_p) * beta_p * (k_r . k_p)      for p < r  (strict lower)
  delta  = (I + A)^{-1} rhs                            (forward substitution)
  S_new  = exp(G_{C-1}) S0 + sum_r exp(G_{C-1}-G_r) beta_r (k_r outer delta_r)
  P[t,r] = exp(G_t - G_r) * beta_r * (q_t . k_r)       for r <= t (lower incl)
  y_t    = exp(G_t) * (q_t^T S0) + (P @ delta)[t]
"""
import numpy as np

np.random.seed(0)
T, dk, dv = 40, 16, 12
C = 8  # chunk size (T need not be a multiple; last chunk is short)

k = np.random.randn(T, dk).astype(np.float64)
q = np.random.randn(T, dk).astype(np.float64)
v = np.random.randn(T, dv).astype(np.float64)
k /= np.linalg.norm(k, axis=1, keepdims=True)  # L2-normalized like the kernel
q /= np.linalg.norm(q, axis=1, keepdims=True)
alpha = np.random.uniform(0.85, 0.999, size=T)  # decay in (0,1)
beta = np.random.uniform(0.1, 0.9, size=T)
S0 = np.random.randn(dk, dv).astype(np.float64) * 0.1


def golden():
    S = S0.copy()
    y = np.zeros((T, dv))
    for t in range(T):
        kS = k[t] @ S
        delta = v[t] - alpha[t] * kS
        S = alpha[t] * S + beta[t] * np.outer(k[t], delta)
        y[t] = q[t] @ S
    return y, S


def chunked():
    g = np.log(alpha)
    S = S0.copy()
    y = np.zeros((T, dv))
    for c0 in range(0, T, C):
        c1 = min(c0 + C, T)
        n = c1 - c0
        kk = k[c0:c1]
        qq = q[c0:c1]
        vv = v[c0:c1]
        bb = beta[c0:c1]
        G = np.cumsum(g[c0:c1])  # inclusive cumulative log-decay
        # rhs
        KS0 = kk @ S  # [n, dv]
        rhs = vv - np.exp(G)[:, None] * KS0
        # A[r,p] = exp(G_r-G_p) beta_p (k_r.k_p), strict lower
        KK = kk @ kk.T  # [n, n]
        A = np.zeros((n, n))
        for r in range(n):
            for p in range(r):
                A[r, p] = np.exp(G[r] - G[p]) * bb[p] * KK[r, p]
        # delta = (I + A)^{-1} rhs  via forward substitution
        delta = np.zeros((n, dv))
        for r in range(n):
            delta[r] = rhs[r] - A[r, :r] @ delta[:r]
        # state update
        Snew = np.exp(G[-1]) * S
        for r in range(n):
            Snew += np.exp(G[-1] - G[r]) * bb[r] * np.outer(kk[r], delta[r])
        # output: y_t = exp(G_t)(q_t^T S0) + sum_{r<=t} exp(G_t-G_r) bb_r (q_t.k_r) delta_r
        QK = qq @ kk.T  # [n, n]
        P = np.zeros((n, n))
        for t in range(n):
            for r in range(t + 1):
                P[t, r] = np.exp(G[t] - G[r]) * bb[r] * QK[t, r]
        QS0 = qq @ S  # [n, dv]
        yc = np.exp(G)[:, None] * QS0 + P @ delta
        y[c0:c1] = yc
        S = Snew
    return y, S


yg, Sg = golden()
yc, Sc = chunked()
print("y   max abs diff:", np.max(np.abs(yg - yc)))
print("y   l2_rel:", np.linalg.norm(yg - yc) / np.linalg.norm(yg))
print("S   max abs diff:", np.max(np.abs(Sg - Sc)))
print("S   l2_rel:", np.linalg.norm(Sg - Sc) / np.linalg.norm(Sg))
