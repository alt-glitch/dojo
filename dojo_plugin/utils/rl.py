import hashlib
import logging
import pathlib
import queue
import secrets
import threading
import time

import docker
import docker.errors
import docker.types
from flask import current_app

from ..config import HOST_DATA_PATH, SECCOMP, RL_MAX_INSTANCES, RL_WARM_POOL_SIZE
from . import resolved_tar
from .workspace import exec_run

logger = logging.getLogger(__name__)

POOL_DUMMY_FLAG = "rl_pool_placeholder"


def _insert_flag_via_stdin(container, flag):
    flag = f"pwn.college{{{flag}}}"
    if "localhost" in container.client.api.base_url:
        socket = container.attach_socket(params=dict(stdin=1, stream=1))
        socket._sock.sendall(flag.encode() + b"\n")
        socket.close()
    else:
        ws = container.attach_socket(params=dict(stdin=1, stream=1), ws=True)
        ws.send_text(f"{flag}\n")
        ws.close()


def rl_redis_client():
    import redis
    return redis.from_url(current_app.config["REDIS_URL"])


def rl_docker_client():
    return docker.from_env()


class RLInstanceManager:
    def __init__(self):
        self._warm_pool = queue.Queue()
        self._pool_counter = 0
        self._pool_thread = None

    def init_slots(self):
        r = rl_redis_client()
        active = r.keys("rl:instance:*:flag")
        active_slots = {int(k.decode().split(":")[2]) for k in active}
        all_slots = set(range(RL_MAX_INSTANCES))
        available = all_slots - active_slots
        r.delete("rl:available_slots")
        if available:
            r.sadd("rl:available_slots", *available)

    def allocate_slot(self):
        r = rl_redis_client()
        slot = r.spop("rl:available_slots")
        if slot is None:
            return None
        return int(slot)

    def release_slot(self, slot):
        r = rl_redis_client()
        r.delete(f"rl:instance:{slot}:flag")
        r.delete(f"rl:instance:{slot}")
        r.sadd("rl:available_slots", slot)

    def store_instance(self, slot, challenge_key, dojo_id, module_id, challenge_id, flag):
        r = rl_redis_client()
        r.hset(f"rl:instance:{slot}", mapping={
            "challenge_key": challenge_key,
            "dojo_id": dojo_id,
            "module_id": module_id,
            "challenge_id": challenge_id,
            "created_at": str(int(time.time())),
        })
        r.set(f"rl:instance:{slot}:flag", flag)

    def get_instance(self, slot):
        r = rl_redis_client()
        data = r.hgetall(f"rl:instance:{slot}")
        if not data:
            return None
        flag = r.get(f"rl:instance:{slot}:flag")
        result = {k.decode(): v.decode() for k, v in data.items()}
        result["flag"] = flag.decode() if flag else None
        result["slot"] = slot
        return result

    def list_instances(self):
        r = rl_redis_client()
        instances = []
        docker_client = rl_docker_client()
        for slot in range(RL_MAX_INSTANCES):
            data = r.hgetall(f"rl:instance:{slot}")
            if not data:
                continue
            info = {k.decode(): v.decode() for k, v in data.items()}
            info["slot"] = slot
            container_name = f"rl_{slot}"
            try:
                container = docker_client.containers.get(container_name)
                info["status"] = container.status
            except docker.errors.NotFound:
                info["status"] = "not_found"
            instances.append(info)
        return instances

    def _get_image_path(self, docker_client, image_name):
        image = docker_client.images.get(image_name)
        image_env = image.attrs["Config"].get("Env") or []
        for env_var in image_env:
            if env_var.startswith("PATH="):
                return env_var[len("PATH="):]
        return "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    def create_rl_container(self, slot, image_name="pwncollege/challenge-legacy:latest"):
        docker_client = rl_docker_client()
        container_name = f"rl_{slot}"

        try:
            old = docker_client.containers.get(container_name)
            old.remove(force=True)
        except docker.errors.NotFound:
            pass

        challenge_bin_path = "/run/challenge/bin"
        dojo_bin_path = "/run/dojo/bin"
        image_path = self._get_image_path(docker_client, image_name)
        env_path = f"{challenge_bin_path}:{dojo_bin_path}:{image_path}"

        container = docker_client.containers.create(
            image=image_name,
            entrypoint=[
                "/nix/var/nix/profiles/dojo-workspace/bin/dojo-init",
                f"{dojo_bin_path}/sleep",
                "infinity",
            ],
            name=container_name,
            hostname=f"rl-{slot}",
            user="0",
            working_dir="/home/hacker",
            environment={
                "HOME": "/home/hacker",
                "PATH": env_path,
                "SHELL": f"{dojo_bin_path}/bash",
            },
            labels={
                "dojo.rl": "true",
                "dojo.rl_slot": str(slot),
            },
            mounts=[
                docker.types.Mount(
                    "/nix",
                    f"{HOST_DATA_PATH}/workspace/nix",
                    "bind",
                    read_only=True,
                ),
                docker.types.Mount(
                    "/run/dojo/sys",
                    "/run/dojo/dojofs",
                    "bind",
                    read_only=True,
                    propagation="slave",
                ),
            ],
            detach=True,
            stdin_open=True,
            init=True,
            auto_remove=True,
            cap_add=["SYS_PTRACE"],
            mem_limit="1G",
            cpu_period=100000,
            cpu_quota=100000,
            pids_limit=256,
            security_opt=[f"seccomp={SECCOMP}"],
        )

        container.start()

        start_time = time.time()
        for line in container.logs(stream=True, follow=True):
            if b"DOJO_INIT_INITIALIZED" in line:
                logger.info(f"rl_{slot} initialized in {time.time()-start_time:.1f}s")
                break
        else:
            raise RuntimeError(f"rl_{slot} failed to initialize")

        return container

    def insert_rl_challenge(self, container, dojo_challenge, variant_seed):
        def is_option_path(path):
            path = pathlib.Path(*path.parts[:len(dojo_challenge.path.parts) + 1])
            return path.name.startswith("_") and path.is_dir()

        exec_run("/run/dojo/bin/mkdir -p /challenge", container=container)

        root_dir = dojo_challenge.path.parent.parent
        challenge_tar = resolved_tar(
            dojo_challenge.path,
            root_dir=root_dir,
            filter=lambda path: not is_option_path(path),
        )
        container.put_archive("/challenge", challenge_tar)

        option_paths = sorted(
            path for path in dojo_challenge.path.iterdir() if is_option_path(path)
        )
        if option_paths:
            secret = current_app.config["SECRET_KEY"]
            option_hash = hashlib.sha256(
                f"{secret}_{variant_seed}_{dojo_challenge.challenge_id}".encode()
            ).digest()
            option = option_paths[
                int.from_bytes(option_hash[:8], "little") % len(option_paths)
            ]
            container.put_archive("/challenge", resolved_tar(option, root_dir=root_dir))

        exec_run(
            r"/run/dojo/bin/find /challenge/ -mindepth 1 -exec /run/dojo/bin/chown root:root {} \;",
            container=container,
        )
        exec_run(
            r"/run/dojo/bin/find /challenge/ -mindepth 1 -exec /run/dojo/bin/chmod 4755 {} \;",
            container=container,
        )

    def _inject_flag_via_exec(self, container, flag):
        exec_run(
            f"/run/dojo/bin/sh -c 'echo \"{flag}\" | /run/dojo/bin/install -m 400 /dev/stdin /flag'",
            container=container,
        )

    def create_instance(self, dojo_challenge, variant=None):
        slot = self.allocate_slot()
        if slot is None:
            raise RuntimeError(f"No available slots (max {RL_MAX_INSTANCES})")

        try:
            from_pool = False
            warm_container = None
            if RL_WARM_POOL_SIZE > 0:
                try:
                    warm_container = self._warm_pool.get_nowait()
                except queue.Empty:
                    pass

            if warm_container:
                try:
                    warm_container.rename(f"rl_{slot}")
                    container = warm_container
                    from_pool = True
                except Exception:
                    try:
                        warm_container.remove(force=True)
                    except docker.errors.NotFound:
                        pass
                    container = self.create_rl_container(slot)
                self._trigger_replenish()
            else:
                container = self.create_rl_container(slot)

            variant_seed = variant if variant is not None else slot
            resolved = dojo_challenge.resolve()
            if dojo_challenge.path.exists() and not resolved.image.startswith("challenges.pwn.college/"):
                self.insert_rl_challenge(container, dojo_challenge, variant_seed)

            flag_token = secrets.token_hex(16)
            flag = f"pwn.college{{{flag_token}}}"

            if from_pool:
                self._inject_flag_via_exec(container, flag)
                if dojo_challenge.path.exists():
                    challenge_init = dojo_challenge.path / ".init"
                    module_init = dojo_challenge.path.parent / ".init"
                    if challenge_init.exists() or module_init.exists():
                        exec_run("/challenge/.init", container=container, assert_success=False)
            else:
                _insert_flag_via_stdin(container, flag_token)
                for line in container.logs(stream=True, follow=True):
                    if b"DOJO_INIT_READY" in line:
                        break
                    if b"DOJO_INIT_FAILED:" in line:
                        raise RuntimeError(f"rl_{slot} init failed: {line}")
                else:
                    raise RuntimeError(f"rl_{slot} failed to become ready")

            challenge_key = f"{dojo_challenge.module.id}/{dojo_challenge.id}"
            self.store_instance(
                slot,
                challenge_key=challenge_key,
                dojo_id=dojo_challenge.dojo.reference_id,
                module_id=dojo_challenge.module.id,
                challenge_id=dojo_challenge.id,
                flag=flag,
            )

            return {
                "slot": slot,
                "ssh_user": f"rl_{slot}",
                "challenge": dojo_challenge.id,
                "module": dojo_challenge.module.id,
                "dojo": dojo_challenge.dojo.reference_id,
            }
        except Exception:
            self.release_slot(slot)
            try:
                docker_client = rl_docker_client()
                docker_client.containers.get(f"rl_{slot}").remove(force=True)
            except docker.errors.NotFound:
                pass
            raise

    def destroy_instance(self, slot):
        docker_client = rl_docker_client()
        try:
            container = docker_client.containers.get(f"rl_{slot}")
            container.remove(force=True)
        except docker.errors.NotFound:
            pass
        self.release_slot(slot)

    def reset_instance(self, slot, dojo_challenge=None):
        old_info = self.get_instance(slot)
        if not old_info:
            raise KeyError(f"No instance at slot {slot}")

        self.destroy_instance(slot)

        if dojo_challenge is None:
            from ..models import DojoChallenges, DojoModules
            old_module = old_info["module_id"]
            old_challenge = old_info["challenge_id"]
            dojo_challenge = (
                DojoChallenges.query
                .filter_by(id=old_challenge)
                .join(DojoModules.query.filter_by(id=old_module).subquery())
                .first()
            )
            if not dojo_challenge:
                raise ValueError(f"Challenge {old_module}/{old_challenge} no longer exists")

        r = rl_redis_client()
        r.sadd("rl:available_slots", slot)

        return self.create_instance(dojo_challenge)

    def check_flag(self, slot, submitted_flag):
        r = rl_redis_client()
        stored = r.get(f"rl:instance:{slot}:flag")
        if stored is None:
            return False
        return submitted_flag.strip() == stored.decode()

    # --- Warm Pool ---

    def start_warm_pool(self):
        if RL_WARM_POOL_SIZE <= 0:
            return
        self._replenish_event = threading.Event()
        self._pool_thread = threading.Thread(target=self._replenish_loop, daemon=True)
        self._pool_thread.start()
        logger.info(f"Warm pool started (target size: {RL_WARM_POOL_SIZE})")

    def _trigger_replenish(self):
        if hasattr(self, "_replenish_event"):
            self._replenish_event.set()

    def _replenish_loop(self):
        while True:
            while self._warm_pool.qsize() < RL_WARM_POOL_SIZE:
                try:
                    container = self._create_pool_container()
                    self._warm_pool.put(container)
                    logger.info(f"Warm pool: added container (pool size: {self._warm_pool.qsize()})")
                except Exception:
                    logger.exception("Warm pool: failed to create container")
                    time.sleep(5)
            self._replenish_event.wait(timeout=30)
            self._replenish_event.clear()

    def _create_pool_container(self):
        docker_client = rl_docker_client()
        self._pool_counter += 1
        name = f"rl_pool_{self._pool_counter}"

        image_name = "pwncollege/challenge-legacy:latest"
        image_path = self._get_image_path(docker_client, image_name)
        env_path = f"/run/challenge/bin:/run/dojo/bin:{image_path}"

        container = docker_client.containers.create(
            image=image_name,
            entrypoint=[
                "/nix/var/nix/profiles/dojo-workspace/bin/dojo-init",
                "/run/dojo/bin/sleep",
                "infinity",
            ],
            name=name,
            hostname="rl-pool",
            user="0",
            working_dir="/home/hacker",
            environment={
                "HOME": "/home/hacker",
                "PATH": env_path,
                "SHELL": "/run/dojo/bin/bash",
            },
            labels={"dojo.rl": "true", "dojo.rl_pool": "true"},
            mounts=[
                docker.types.Mount(
                    "/nix",
                    f"{HOST_DATA_PATH}/workspace/nix",
                    "bind",
                    read_only=True,
                ),
                docker.types.Mount(
                    "/run/dojo/sys",
                    "/run/dojo/dojofs",
                    "bind",
                    read_only=True,
                    propagation="slave",
                ),
            ],
            detach=True,
            stdin_open=True,
            init=True,
            auto_remove=True,
            cap_add=["SYS_PTRACE"],
            mem_limit="1G",
            cpu_period=100000,
            cpu_quota=100000,
            pids_limit=256,
            security_opt=[f"seccomp={SECCOMP}"],
        )

        container.start()

        for line in container.logs(stream=True, follow=True):
            if b"DOJO_INIT_INITIALIZED" in line:
                break
        else:
            container.remove(force=True)
            raise RuntimeError("Pool container failed to initialize")

        _insert_flag_via_stdin(container, POOL_DUMMY_FLAG)

        for line in container.logs(stream=True, follow=True):
            if b"DOJO_INIT_READY" in line:
                break
        else:
            container.remove(force=True)
            raise RuntimeError("Pool container failed to become ready")

        return container


rl_manager = RLInstanceManager()
