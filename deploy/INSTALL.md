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

On the machine that builds or starts the agent container. This key can only run the broker, so
it is not kept in `~/.ssh`, which the sandbox masks:

```sh
mkdir -p ~/.config/csub && chmod 700 ~/.config/csub
ssh-keygen -t ed25519 -N "" -f ~/.config/csub/agent_key -C csub-agent
```

On the submit host, allow this key to run the broker and nothing else:

```sh
echo "restrict,command=\"$HOME/.local/bin/csub-broker\" $(cat agent_key.pub)" >> ~/.ssh/authorized_keys
```

(Copy `agent_key.pub` to the submit host first.)

## 5. Set up the agent container

The agent container is the sandbox the agent (Claude Code, opencode) runs in, started with
`sandbox-run.sh` (bwrap, on a workstation or login node) or `podman-run.sh` (on a compute node)
from agentic-sandbox. If the agent runs on a plain machine without a sandbox, do this step there
instead.

The sandbox binds only the system toolchain and the current directory, starts from an empty
environment, and always masks `~/.ssh`. So the durable client settings go in a file next to the
agent key, and the directory is bound into every launch:

```sh
cat > ~/.config/csub/client.toml <<'EOF'
ssh_host = "submit.example.org"
ssh_key  = "~/.config/csub/agent_key"
# The sandbox only has an HTTP CONNECT proxy ($http_proxy); send ssh through it.
# -F/dev/null: skip /etc/ssh, whose root-owned files ssh rejects inside the sandbox's user namespace.
# The sandbox masks ~/.ssh, so the host key lives here too (ssh-keyscan below).
ssh_opts = [
  "-F/dev/null",
  "UserKnownHostsFile=~/.config/csub/known_hosts",
  "ProxyCommand=python3 -m csub.transport.httpconnect %h %p",
]
EOF
ssh-keyscan submit.example.org > ~/.config/csub/known_hosts
```

Only the mounts change per launch, because they must be exactly the paths this container has:

```sh
SANDBOX=~/.local/share/csub/agentic-sandbox/scripts
cd /data/lab/project
export CSUB_MOUNTS="$PWD:rw"
"$SANDBOX"/sandbox-run.sh --ro ~/.local --ro ~/.config/csub --env CSUB_MOUNTS \
    --allow submit.example.org -- csub probe
```

- `--ro ~/.local` makes a host `pip install --user '/path/to/csub[mcp]'` (Python 3.10 or newer)
  visible inside. Alternatively bake csub into the image.
- `--ro ~/.config/csub` brings in the key and `client.toml`.
- `--allow submit.example.org` is the only network the container needs.

Once `csub probe` answers, start the agent with the same flags and `claude` (or `opencode`) as
the command, with the MCP server in its configuration:

```json
{"mcpServers": {"csub": {"command": "csub-mcp"}}}
```

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
