#!/bin/bash
# Fake of agentic-sandbox's scripts/sandbox-run.sh for tests: same flag shape, no bwrap.
#
# Records the parsed invocation as one JSON line (appended to $CSUB_FAKE_SANDBOX_LOG when
# set, and always written to $PWD/sandbox-run.json), scrubs the environment the way bwrap
# would, then runs the command directly. CSUB_FAKE_SANDBOX_FAIL_RC=N simulates a sandbox
# start failure (125 ~ image pull failure).
set -euo pipefail

IMAGE=""
RO=()
RW=()
ALLOW=()
GPU=0
KEEP_ID=0
SCRATCH=0
CLAUDE=0
OPENCODE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
        --ro) RO+=("$2"); shift 2 ;;
    --rw) RW+=("$2"); shift 2 ;;
    --allow) ALLOW+=("$2"); shift 2 ;;
            --scratch) SCRATCH=1; RW+=("/scratch/$(id -un)"); shift ;;
    --claude) CLAUDE=1; shift ;;
    --opencode) OPENCODE=1; shift ;;
    -h|--help) echo "fake sandbox-run.sh"; exit 0 ;;
    --) shift; break ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
CMD=("$@")
if [[ ${#CMD[@]} -eq 0 ]]; then
  echo "No command given after --" >&2; exit 1
fi

python3 -I - "$IMAGE" "$GPU" "$KEEP_ID" "$SCRATCH" "$CLAUDE" "$OPENCODE" \
  "${CSUB_FAKE_SANDBOX_LOG:-}" "$PWD" "${#RO[@]}" "${#RW[@]}" "${#ALLOW[@]}" \
  "${RO[@]+"${RO[@]}"}" "${RW[@]+"${RW[@]}"}" "${ALLOW[@]+"${ALLOW[@]}"}" "${CMD[@]}" <<'PY'
import json, os, sys
a = sys.argv[1:]
image, gpu, keep, scratch, claude, opencode, logfile, cwd = a[:8]
n_ro, n_rw, n_allow = (int(x) for x in a[8:11])
rest = a[11:]
ro, rest = rest[:n_ro], rest[n_ro:]
rw, rest = rest[:n_rw], rest[n_rw:]
allow, cmd = rest[:n_allow], rest[n_allow:]
rec = {
    "script": "sandbox-run.sh", "image": image, "ro": ro, "rw": rw, "allow": allow,
    "gpu": gpu == "1", "keep_id": keep == "1", "scratch": scratch == "1",
    "claude": claude == "1", "opencode": opencode == "1",
    "cmd": cmd, "cwd": cwd, "pid": os.getppid(),
    "env": {k: os.environ.get(k) for k in ("LSB_JOBID", "LSB_DJOB_NUMPROC", "CUDA_VISIBLE_DEVICES")},
}
line = json.dumps(rec)
if logfile:
    with open(logfile, "a") as f:
        f.write(line + "\n")
with open(os.path.join(cwd, "sandbox-run.json"), "w") as f:
    f.write(line + "\n")
PY

if [[ -n "${CSUB_FAKE_SANDBOX_FAIL_RC:-}" ]]; then
  echo "Error: fake bwrap failure (rc ${CSUB_FAKE_SANDBOX_FAIL_RC})" >&2
  exit "${CSUB_FAKE_SANDBOX_FAIL_RC}"
fi

# bwrap --clearenv: only PATH/HOME/USER and friends survive.
ENV=(env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
     "HOSTNAME=$(hostname)" "TERM=${TERM:-dumb}")
ENV+=("HOME=$HOME")

if [[ ${#ALLOW[@]} -gt 0 ]]; then
  # Mirror the real script's indirection through a second shell when --allow is used.
  HOSTS=$(IFS=,; echo "${ALLOW[*]}")
  exec "${ENV[@]}" http_proxy=http://127.0.0.1:1 https_proxy=http://127.0.0.1:1 \
    "CSUB_FAKE_ALLOW_HOSTS=$HOSTS" /bin/bash -c 'exec "$@"' bash "${CMD[@]}"
else
  exec "${ENV[@]}" "${CMD[@]}"
fi
