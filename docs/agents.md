# AGENTS

This file provides guidance to AI agents when working with code in this repository.

## Overview

The pwn.college DOJO is a cybersecurity education platform built as a comprehensive CTFd plugin.
It provides isolated Docker-based workspace environments for hands-on security challenges.
The DOJO runs in a docker-in-docker setting, with the "outer" container using docker-compose to spin up "inner" containers running infrastructure components.

The DOJO also supports an **RL mode** for reinforcement learning agents — a separate API layer that creates challenge containers without user accounts, using slot-based identity and SSH routing.

## Common Development Commands

> **Note:** `deploy.sh` is for **development only**. For production deployment, see `docs/deployment.md`.

### Quick Development Setup

```bash
# Start up the dojo
./deploy.sh

# (Re)start the dojo, and run all testcases
./deploy.sh -t

# Run the testcases (without restarting the dojo)
./deploy.sh -N -t

# Get container details
DOJO_CONTAINER=$(basename "$PWD")

# access the web instance
DOJO_IP=$(docker inspect "$DOJO_CONTAINER" | jq -r '.[0].NetworkSettings.Networks.bridge.IPAddress')
curl "http://$DOJO_IP"

# get CTFd logs
docker exec "$DOJO_CONTAINER" docker logs ctfd

# interact with docker-compose with the correct settings
docker exec "$DOJO_CONTAINER" dojo compose ps

# run DB queries against DOJO's postgresql database
docker exec -i "$DOJO_CONTAINER" dojo db

# run python in the DOJO's CTFd context
docker exec -i "$DOJO_CONTAINER" dojo flask

# enter a learner's container (must be started first via a testcase or the web interface)
docker exec -i "$DOJO_CONTAINER" dojo enter USER_ID

# run an individual testcase (needs docker socket)
docker run -v /var/run/docker.sock:/var/run/docker.sock -v $PWD:/opt/pwn.college -e "DOJO_CONTAINER=dojo" dojo-test pytest -v /opt/pwn.college/test/test_dojos.py::test_create_dojo
```

### Starting with RL Mode

```bash
# Generate an SSH key for RL agents (or use an existing one)
ssh-keygen -t ed25519 -f test/rl_test_key -N "" -C "rl-test-key"

# Start with RL enabled (development)
./deploy.sh -e RL_ENABLED=True -e RL_MAX_INSTANCES=4 -e "RL_SSH_PUBLIC_KEY=$(cat test/rl_test_key.pub)"

# Start with RL enabled (production-style)
docker run --name dojo --privileged \
    -v "$PWD:/opt/pwn.college" -v "$PWD/data:/data" \
    -e RL_ENABLED=True -e RL_MAX_INSTANCES=128 \
    -e "RL_SSH_PUBLIC_KEY=$(cat path/to/agent_key.pub)" \
    -p 2222:22 -p 80:80 -p 443:443 \
    -d pwncollege/dojo

# After force-recreating inner services (if env vars changed):
docker exec dojo dojo compose up -d --force-recreate ctfd sshd nginx

# Rebuild sshd after modifying sshd/ files (auth.py, enter.py, Dockerfile):
docker exec dojo dojo compose build sshd && docker exec dojo dojo compose up -d sshd
```

### Troubleshooting

Container start failures show up in the ctfd container logs:
```bash
docker exec "$DOJO_CONTAINER" docker logs ctfd
```

RL-specific:
- **RL endpoints return 404**: `RL_ENABLED` env var not reaching ctfd. Force-recreate: `dojo compose up -d --force-recreate ctfd nginx`
- **SSH "Permission denied"**: sshd container has stale code. Rebuild: `dojo compose build sshd && dojo compose up -d sshd`
- **SSH key not accepted**: Check `/etc/environment` inside sshd container for `RL_SSH_PUBLIC_KEY`. If missing, force-recreate sshd.
- **403 on POST to RL API**: The CSRF bypass in `__init__.py` isn't loaded. Restart ctfd.
- **Container creation fails**: Check that `pwncollege/challenge-legacy:latest` image exists inside the DinD docker. Run `docker exec dojo docker images`.

### Testing

```bash
# Restart the dojo and run all tests
./deploy.sh -t

# Run the testcases again (without restarting the dojo)
./deploy.sh -N -t

# Run tests without using docker or workspace cache
./deploy.sh -D "" -W "" -t

# Run an individual testcase (needs docker socket)
docker run -v /var/run/docker.sock:/var/run/docker.sock -v $PWD:/opt/pwn.college -e "DOJO_CONTAINER=dojo" dojo-test pytest -v /opt/pwn.college/test/test_dojos.py::test_create_dojo

# Run RL smoke test
./test/test_rl_smoke.sh

# Run RL pytest (requires RL_ENABLED=True in running dojo)
./deploy.sh -N -e RL_ENABLED=True -e RL_MAX_INSTANCES=4 -e "RL_SSH_PUBLIC_KEY=$(cat test/rl_test_key.pub)" -t -- test/test_rl.py -v
```

**Test Script Options:**
- `-r DB_BACKUP`: Restore database backup before testing
- `-c CONTAINER_NAME`: Custom container name (default: <dirname>)
- `-D DOCKER_DIR`: Persistent Docker directory (avoids rebuilds)
- `-W WORKSPACE_DIR`: Persistent workspace directory (avoids rebuilds)
- `-T`: Skip running tests (only setup environment)
- `-N`: Skip startup (just run tests)
- `-p`: Export ports (80->80, 443->443, 22->2222)
- `-e ENV_VAR=value`: Set environment variables
- `-b`: Build Docker image locally


## High-Level Architecture

### Nested Docker Architecture
The system uses a sophisticated nested Docker setup:
- Outer container runs all infrastructure (CTFd, database, nginx, etc.)
- Inner Docker-in-Docker daemon manages isolated user workspace containers
- This provides strong security isolation between infrastructure and user environments

### Key Components

1. **CTFd Plugin** (`/dojo_plugin/`)
   - Core application logic as CTFd plugin
   - API endpoints in `api/`
   - Database models in `models/`
   - Page controllers in `pages/`
   - RL mode in `api/v1/rl.py`, `utils/rl.py`, `pages/rl.py`

2. **Theme** (`/dojo_theme/`)
   - Custom UI replacing most CTFd frontend
   - Static assets in `static/`
   - Templates in `templates/`
   - RL dashboard in `templates/rl_dashboard.html`

3. **Workspace** (`/workspace/`)
   - Nix-based tool provisioning
   - User container configuration
   - Security tools and development environment

4. **SSH Service** (`/sshd/`)
   - Custom SSH daemon for user and RL access
   - `auth.py`: authenticates via database (users) or prebaked key (RL)
   - `enter.py`: `docker exec` into user or RL containers
   - `sshd_config`: routes `hacker` to normal flow, `rl_*` to RL flow
   - Bind-mounted from host (`/opt/pwn.college/sshd:/opt/sshd:ro`), so code changes take effect without rebuild

5. **RL SDK** (`/rl_sdk.py`)
   - Async/sync client for the RL API
   - Admin operations (load dojos, promote)
   - `EpisodePool` for parallel episode collection

### Data Storage

Inside the "outer" component:

- `/data/` - All persistent data
- `/data/homes/` - User home directories (btrfs subvolumes, 1GB limit)
- `/data/dojos/` - Dojo challenge definitions
- `/data/workspace/nix/` - Nix store for tools
- `/data/postgres/` - Database files
- `/data/config.env` - Persisted configuration (do NOT put values with spaces here — use docker run `-e` instead)

### Container Services
The docker-compose.yml defines these services:
- `db` - PostgreSQL database
- `cache` - Redis cache
- `ctfd` - Main CTFd application
- `nginx` - Reverse proxy with SSL
- `sshd` - SSH access service (bind-mounted from host)
- `homefs` - Home directory management
- `workspacefs` - Workspace filesystem overlay
- Monitoring stack (Prometheus, Grafana, Splunk)

### Security Model
- Challenges run as setuid binaries
- Flag at `/flag` readable only by root
- User runs as `hacker` (UID 1000)
- Custom seccomp profiles for containers
- Network isolation between user containers

### Workspace Environment
- Tools provided via Nix overlay at `/nix`
- Mounted in `/run/dojo/` inside user containers
- On-demand services: VSCode (`code`), Desktop (`desktop`), ttyd (`terminal`)
- 6-hour timeout for idle containers (RL containers use `sleep infinity`)

## RL Mode Architecture

### Overview
RL mode adds a user-agnostic API layer for reinforcement learning agents. Containers are identified by slot numbers (0 to `RL_MAX_INSTANCES-1`) instead of user accounts. A single prebaked SSH key authenticates all agents; routing is by SSH username (`rl_0`, `rl_1`, ...).

### Configuration
| Env Var | Default | Where Used | Purpose |
|---------|---------|------------|---------|
| `RL_ENABLED` | `False` | ctfd | Enables RL API endpoints and dashboard |
| `RL_MAX_INSTANCES` | `128` | ctfd | Max concurrent challenge containers |
| `RL_WARM_POOL_SIZE` | `0` | ctfd | Pre-warmed containers (0 = disabled) |
| `RL_SSH_PUBLIC_KEY` | (empty) | sshd | SSH public key for RL agent auth |

Config propagation: outer container env → `dojo-init` writes to `/data/config.env` (except `RL_SSH_PUBLIC_KEY` which has spaces) → docker-compose `x-ctfd-env` anchor → inner containers.

**Important**: `RL_SSH_PUBLIC_KEY` is NOT persisted in `config.env` because the `define` function doesn't handle values with spaces. It must be passed via `docker run -e` every time, and reaches sshd via docker-compose env var.

### API Endpoints (no auth required)
All under `/pwncollege_api/v1/rl/`:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/status` | Running count, max capacity |
| GET | `/challenges` | All available challenges |
| POST | `/instances` | Create instance `{challenge: "module/id"}` |
| GET | `/instances` | List running instances |
| GET | `/instances/<slot>` | Details including flag |
| POST | `/instances/<slot>/check` | Check flag `{flag: "pwn.college{...}"}` |
| POST | `/instances/<slot>/reset` | Destroy + recreate (new flag) |
| DELETE | `/instances/<slot>` | Destroy instance |

CSRF is bypassed for `/pwncollege_api/v1/rl/` paths (see `handle_authorization` and `rl_csrf_wrapper` in `__init__.py`).

### SSH Routing
```
Agent SSH as rl_42@host → sshd matches "Match User rl_*" →
auth.py returns prebaked key with command="enter.py rl_42" →
enter.py uses local Docker socket → docker exec into rl_42 container
```

The sshd container has 256 pre-created users (`rl_0` through `rl_255`), each in the `docker` group.

### RL Container vs Normal Container
| Aspect | Normal (user) | RL (slot) |
|--------|--------------|-----------|
| Identity | `user_{user_id}` | `rl_{slot}` |
| Home dir | btrfs subvolume (persistent) | Ephemeral (container-local) |
| Network | `workspace_net` with assigned IP | Default bridge |
| Timeout | `sleep 6h` | `sleep infinity` |
| Resources | 4GB RAM, 4 CPUs | 1GB RAM, 1 CPU |
| Flag | Per-user cryptographic | Random token per instance |
| Auth token | JWT-based | None |
| SSH routing | Key → DB lookup → user_id | Key → prebaked → slot name |

### Key Files
| File | Purpose |
|------|---------|
| `dojo_plugin/api/v1/rl.py` | RL API endpoints (Flask-RESTX namespace) |
| `dojo_plugin/utils/rl.py` | `RLInstanceManager`: slot allocation, container creation, warm pool |
| `dojo_plugin/pages/rl.py` | Dashboard route (`/admin/rl`) |
| `dojo_theme/templates/rl_dashboard.html` | Dashboard UI |
| `sshd/auth.py` | SSH key lookup — handles both `hacker` (DB) and `rl_*` (prebaked key) |
| `sshd/enter.py` | SSH entry — `rl_*` containers use local Docker, skip workspace node resolution |
| `sshd/sshd_config` | Two Match blocks: `User hacker` and `User rl_*` |
| `rl_sdk.py` | Client SDK for RL API (async + sync) |

### Warm Pool
When `RL_WARM_POOL_SIZE > 0`, a background thread pre-creates containers through the full `dojo-init` flow with a dummy flag. On instance creation:
1. Take from pool (instant) or create fresh (~8s)
2. Rename container to `rl_{slot}`
3. Inject challenge via `put_archive` + `chmod 4755`
4. Overwrite `/flag` via `exec_run` (stdin already consumed)
5. Run `.init` if exists

**Critical**: warm pool containers have stdin consumed and `DOJO_INIT_READY` already emitted. The `create_instance` method has two distinct code paths for fresh vs pool containers.

### State Management
- **Slot allocation**: Redis set `rl:available_slots`
- **Instance metadata**: Redis hash `rl:instance:{slot}` (challenge_key, dojo_id, module_id, challenge_id, created_at)
- **Flag storage**: Redis key `rl:instance:{slot}:flag`
- **Container tracking**: Docker labels (`dojo.rl=true`, `dojo.rl_slot={slot}`)

### Gotchas and Lessons Learned
- **Circular imports**: `utils/rl.py` cannot import from `api/v1/docker.py` (circular via `api/__init__.py`). The `_insert_flag_via_stdin` function is duplicated locally to avoid this.
- **CSRF bypass**: CTFd has TWO CSRF handlers (`tokens` and `csrf`). Both must be bypassed for RL endpoints. The `csrf` handler is wrapped in `__init__.py` with `rl_csrf_wrapper`.
- **RL namespace registration**: The `from .v1.rl import rl_namespace` must be inside the `if config.RL_ENABLED:` block (lazy import), not at module top level, to avoid import-time failures.
- **sshd code**: `auth.py`, `enter.py`, and `sshd_config` are bind-mounted via docker-compose. Changes to these files take effect without rebuilding. But the Dockerfile (user creation, package installation) still requires `dojo compose build sshd`.
- **config.env spaces**: The `define` function in `dojo-init` writes unquoted values. Values with spaces (like SSH public keys) break when sourced. Don't use `define` for such values — pass via `docker run -e` only.
- **Inner service env vars**: After changing env vars, `dojo compose restart` is not enough — the env is baked at container creation. Use `dojo compose up -d --force-recreate <service>` and restart nginx afterward (it caches upstream IPs).

## Adding Configuration

To add a new configuration entry:
1. Add default in `dojo/dojo-init` (skip if value may contain spaces)
2. Add to `x-ctfd-env` anchor in `docker-compose.yml` (and/or sshd environment)
3. Load as global in `dojo_plugin/config.py`
4. Import where needed

## Testing Approach

The project uses pytest with fixtures for:
- User session management
- Dojo creation and loading
- Challenge interaction testing

Run tests with `./deploy.sh -t` which handles container setup and cleanup.
Tests are in `test/test_*.py`, implemented as module-level `test_*` functions, not classes.

RL tests are in `test/test_rl.py` and auto-skip when `RL_ENABLED` is not set.

## Coding Standards

### Comments and Documentation

**DO NOT ADD COMMENTS.**

Comments are only acceptable when they explain non-obvious **why** decisions, complex algorithms, or critical business rules that cannot be understood from the code itself.

Examples of unacceptable comments:
```python
# DON'T DO THIS
# Generate RSA key
# Get user by ID
# Increment counter
# Call the function
```

The only acceptable comments explain critical context that cannot be inferred:
```python
# Exponential penalty: each attempt reduces score by 10%
base_score = 100 * (0.9 ** attempts)

# Docker socket must be mounted at this exact path for Mac compatibility
SOCKET_PATH = "/var/run/docker.sock"
```

Function and variable names must be self-documenting. If you feel the need to add a comment, first consider if better naming would make it unnecessary.
