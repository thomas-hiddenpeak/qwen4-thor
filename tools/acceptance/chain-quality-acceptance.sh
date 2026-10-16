#!/bin/bash
# chain-quality-acceptance.sh — final-quality-business B=12288 (gate:
# quality 11q manifest-exact + base-vs-cand identical, business 6 req
# bit-identical on the C5+C6 binary), then final-acceptance 12288
# (C3 protocol baseline C=0 + candidate, 5 tiers + 261887 target tier,
# 3 runs each, compare + memory gate USER_APPROVED_OVERRUN).
set -u
cd /home/rm01/models/dev/qwen4-thor
W=.q4t-work/moe-residency-20260930
LOG=$W/chain-quality-acceptance.log
log() { echo "[$(date -Is)] $*" | tee -a "$LOG"; }

log "=== chain start; binary $(sha256sum build/q4t | cut -d' ' -f1 | cut -c1-16) commit $(git rev-parse --short HEAD) ==="
bash "$W/final-quality-business.sh" 12288 >> "$LOG" 2>&1
rc=$?
log "final-quality-business rc=$rc"
if [ $rc -ne 0 ]; then
  log "CHAIN-STOP: quality/business verification failed; final-acceptance NOT started"
  exit 1
fi
log "=== quality/business PASS; starting final-acceptance B=12288 ==="
bash "$W/final-acceptance.sh" 12288 >> "$LOG" 2>&1
rc=$?
log "final-acceptance rc=$rc"
log "=== chain done rc=$rc ==="
exit $rc
