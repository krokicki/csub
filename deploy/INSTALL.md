# Installing csub

Replace `submit.example.org`, `/data/lab` and the queue names with your own.

## Prerequisites

- Python 3.9 or newer on the submit host. The same interpreter must exist on the compute nodes,
  because each job restarts the broker there.
- Your home directory is shared between the submit host and the compute nodes.
- Rootless podman works for your account on the compute nodes. Follow the one-time setup in the
  [agentic-sandbox README](https://github.com/JaneliaScientificComputingSystems/agentic-sandbox).

## 1. Build the package

In a checkout of this repository:

```sh
python3 -m pip wheel --no-deps -w dist .
scp dist/csub-0.1.0-py3-none-any.whl submit.example.org:
```

## 2. Install the broker on the submit host

```sh
ssh submit.example.org
pip3 install --user csub-0.1.0-py3-none-any.whl
git clone https://github.com/JaneliaScientificComputingSystems/agentic-sandbox \
    ~/.local/share/csub/agentic-sandbox
```

This installs `~/.local/bin/csub` and `~/.local/bin/csub-broker`.

Every request over ssh starts your login shell on the submit host, and bash reads `~/.bashrc`
even for a non-interactive command. Anything slow in there (a `conda` hook, `nvm`) is paid on
every `csub` call. Keep such lines behind an interactive guard, e.g. `[[ $- == *i* ]] || return`.
Setting `norc = true` in the policy's `[lsf]` table skips `~/.bashrc` for the broker's own
`bsub`/`bjobs`/`bkill` calls, but not for the login shell sshd starts.

## 3. Write the policy

```sh
mkdir -p ~/.config/csub
cat > ~/.config/csub/broker.toml <<'EOF'
[broker]
default_image  = "ghcr.io/janeliascientificcomputingsystems/agentic-sandbox-lite:latest"
allowed_images = ["ghcr.io/janeliascientificcomputingsystems/agentic-sandbox-*:*"]
allowed_roots  = ["/data/lab"]
allowed_hosts  = ["pypi.org", "files.pythonhosted.org"]
env_allow      = ["OMP_NUM_THREADS"]

[limits]
max_slots = 64
max_gpus = 4
max_walltime_min = 2880
max_estimated_cost_usd = 50
slot_price_usd_per_hour = 0.05
cpu_short_queue = "short"
cpu_default_queue = "long"

[queues.short]
mem_per_slot_mb = 8192
max_walltime_min = 60

[queues.long]
mem_per_slot_mb = 8192
max_walltime_min = 2880

[queues.gpu]
mem_per_slot_mb = 16384
max_walltime_min = 1440
gpu = true
slots_per_gpu = 8
gpu_price_usd_per_hour = 0.5

[lsf]
profile = "/etc/profile.d/lsf.sh"   # the file that sets up LSF in a login shell; "" if none
EOF
chmod 600 ~/.config/csub/broker.toml
```

Check it:

```sh
echo '{"protocol":1,"op":"probe"}' | ~/.local/bin/csub-broker
```

The answer contains `"ok":true`. Otherwise it names the policy line that is wrong.

## 4. Create the agent's key

On the machine that builds or starts the agent container:

```sh
ssh-keygen -t ed25519 -N "" -f csub_ed25519 -C csub-agent
```

On the submit host, allow this key to run the broker and nothing else:

```sh
echo "restrict,command=\"$HOME/.local/bin/csub-broker\" $(cat csub_ed25519.pub)" >> ~/.ssh/authorized_keys
```

(Copy `csub_ed25519.pub` to the submit host first.)

## 5. Set up the agent container

```sh
pip install '/path/to/csub[mcp]'        # needs Python 3.10 or newer
cp csub_ed25519 ~/.ssh/csub_ed25519
export CSUB_SSH_HOST=submit.example.org
export CSUB_MOUNTS=/data/lab/project:rw  # exactly the paths mounted into this container
csub probe
```

Add the MCP server to the agent's configuration:

```json
{"mcpServers": {"csub": {"command": "csub-mcp"}}}
```

The container needs network access to the submit host on port 22 and nothing else for csub.

## 6. Submit as yourself on the submit host

To start an agent loop as a job, or to rerun something by hand:

```sh
cd /data/lab/project
CSUB_TRANSPORT=local CSUB_MOUNTS="$PWD:rw" csub submit --walltime 10 -- hostname
```

## 7. Smoke test

```sh
cd /data/lab/project
/path/to/csub/deploy/smoke.sh
```

It runs a CPU job, a GPU job, a nested submission and two requests that must be rejected. The
GPU job uses the policy's GPU queue with the shortest walltime limit; set
`CSUB_SMOKE_GPU_QUEUE` to pick another.
