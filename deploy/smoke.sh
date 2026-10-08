#!/bin/sh
# Smoke test against a real cluster. Run it from a directory inside an allowed root.
# Runs probe, a CPU job, a GPU job, a nested submission and two rejections. The jobs are
# small but real. CSUB_SMOKE_GPU_QUEUE overrides the automatically chosen GPU queue.
set -eu

# On the submit host (bsub on PATH) talk to the broker directly; elsewhere use ssh.
if [ -z "${CSUB_TRANSPORT:-}" ] && command -v bsub >/dev/null 2>&1; then
  export CSUB_TRANSPORT=local
fi
# Without a container launcher nothing sets CSUB_MOUNTS; mount the current directory.
if [ -z "${CSUB_MOUNTS:-}" ]; then
  export CSUB_MOUNTS="$(pwd -P):rw"
fi
echo "transport=${CSUB_TRANSPORT:-ssh} mounts=$CSUB_MOUNTS"

echo "== probe"
csub probe

# submit_id ARGS... -> prints the job id, or the broker's error and exits
submit_id() {
  out=$(csub --json submit "$@") || { echo "$out" | python3 -c 'import json,sys; e=json.load(sys.stdin)["error"]; print(f"{e[\"code\"]}: {e[\"message\"]}")' >&2; exit 1; }
  echo "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])'
}

echo "== CPU job"
JOB=$(submit_id --name smoke-cpu --walltime 5 -- sh -c 'hostname; env | grep ^CSUB_')
csub wait "$JOB" --timeout 1800

# GPU queue: CSUB_SMOKE_GPU_QUEUE, else the policy's GPU queue with the shortest walltime
# limit (usually the fastest to schedule), cheapest first on ties.
GPU_QUEUE=${CSUB_SMOKE_GPU_QUEUE:-$(csub --json probe | python3 -c '
import json, sys
qs = [(q["max_walltime_min"], q.get("gpu_price_usd_per_hour") or 0, n)
      for n, q in json.load(sys.stdin)["queues"].items() if q.get("gpu")]
print(min(qs)[2] if qs else "")')}
if [ -z "$GPU_QUEUE" ]; then
  echo "FAIL: the policy has no GPU queue (set CSUB_SMOKE_GPU_QUEUE)"; exit 1
fi
echo "== GPU job on $GPU_QUEUE"
GJOB=$(submit_id --name smoke-gpu --walltime 10 --gpus 1 -q "$GPU_QUEUE" -- sh -c 'nvidia-smi -L; echo CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES')
csub wait "$GJOB" --timeout 1800

echo "== nested submission"
NJOB=$(submit_id --name smoke-nested --walltime 10 -- sh -c 'csub submit --walltime 5 -- hostname && csub status')
csub wait "$NJOB" --timeout 1800

echo "== expected rejections"
if csub submit --image localhost/evil:1 -- true; then echo "FAIL: localhost image accepted"; exit 1; fi
if csub submit --allow evil.example -- true; then echo "FAIL: unknown host accepted"; exit 1; fi
echo "== all good"