import subprocess
import time
import os

import pytest
import requests

from utils import DOJO_URL, DOJO_SSH_HOST, DOJO_CONTAINER, login, create_dojo, make_dojo_official, dojo_run


def rl_enabled():
    result = dojo_run("docker", "exec", "ctfd", "printenv", "RL_ENABLED", check=False)
    return result.returncode == 0 and result.stdout.strip() == "True"


pytestmark = pytest.mark.skipif(not rl_enabled(), reason="RL mode not enabled")


@pytest.fixture(scope="module")
def rl_dojo(admin_session):
    try:
        rid = create_dojo("pwncollege/example-dojo", session=admin_session)
    except (AssertionError, AssertionError):
        rid = "example"
    make_dojo_official(rid, admin_session)
    return rid


@pytest.fixture(scope="module")
def admin_session():
    return login("admin", "admin")


def test_rl_status():
    response = requests.get(f"{DOJO_URL}/pwncollege_api/v1/rl/status")
    assert response.status_code == 200
    data = response.json()
    assert data["success"]
    assert data["enabled"]
    assert data["max_instances"] > 0


def test_rl_challenges(rl_dojo):
    response = requests.get(f"{DOJO_URL}/pwncollege_api/v1/rl/challenges")
    assert response.status_code == 200
    data = response.json()
    assert data["success"]
    assert len(data["challenges"]) > 0


def test_rl_create_instance(rl_dojo):
    response = requests.post(f"{DOJO_URL}/pwncollege_api/v1/rl/instances", json={
        "challenge": "hello/apple",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["success"], f"Failed: {data.get('error')}"
    assert "slot" in data
    assert "ssh_user" in data
    assert data["ssh_user"].startswith("rl_")

    slot = data["slot"]

    try:
        response = requests.get(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}")
        assert response.status_code == 200
        info = response.json()
        assert info["success"]
        assert info["flag"].startswith("pwn.college{")

        response = requests.get(f"{DOJO_URL}/pwncollege_api/v1/rl/instances")
        assert response.status_code == 200
        listing = response.json()
        assert any(inst["slot"] == slot for inst in listing["instances"])
    finally:
        requests.delete(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}")


def test_rl_check_flag(rl_dojo):
    response = requests.post(f"{DOJO_URL}/pwncollege_api/v1/rl/instances", json={
        "challenge": "hello/apple",
    })
    data = response.json()
    assert data["success"], f"Failed: {data.get('error')}"
    slot = data["slot"]

    try:
        info = requests.get(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}").json()
        real_flag = info["flag"]

        wrong = requests.post(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}/check", json={
            "flag": "pwn.college{wrong}"
        }).json()
        assert not wrong["correct"]

        correct = requests.post(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}/check", json={
            "flag": real_flag
        }).json()
        assert correct["correct"]
    finally:
        requests.delete(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}")


def test_rl_reset_instance(rl_dojo):
    response = requests.post(f"{DOJO_URL}/pwncollege_api/v1/rl/instances", json={
        "challenge": "hello/apple",
    })
    data = response.json()
    assert data["success"], f"Failed: {data.get('error')}"
    slot = data["slot"]

    try:
        old_flag = requests.get(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}").json()["flag"]

        reset = requests.post(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}/reset", json={}).json()
        assert reset["success"], f"Reset failed: {reset.get('error')}"

        new_slot = reset["slot"]
        new_flag = requests.get(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{new_slot}").json()["flag"]
        assert new_flag != old_flag

        slot = new_slot
    finally:
        requests.delete(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}")


def test_rl_destroy_instance(rl_dojo):
    response = requests.post(f"{DOJO_URL}/pwncollege_api/v1/rl/instances", json={
        "challenge": "hello/apple",
    })
    data = response.json()
    assert data["success"], f"Failed: {data.get('error')}"
    slot = data["slot"]

    response = requests.delete(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}")
    assert response.json()["success"]

    info = requests.get(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}").json()
    assert not info["success"]


def test_rl_ssh_routing(rl_dojo):
    response = requests.post(f"{DOJO_URL}/pwncollege_api/v1/rl/instances", json={
        "challenge": "hello/apple",
    })
    data = response.json()
    assert data["success"], f"Failed: {data.get('error')}"
    slot = data["slot"]
    ssh_user = data["ssh_user"]

    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10",
                "-i", "/opt/pwn.college/test/rl_test_key",
                f"{ssh_user}@{DOJO_SSH_HOST}",
                "whoami",
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"SSH failed: {result.stderr}"
        assert "hacker" in result.stdout
    finally:
        requests.delete(f"{DOJO_URL}/pwncollege_api/v1/rl/instances/{slot}")


def test_rl_max_instances_error():
    response = requests.post(f"{DOJO_URL}/pwncollege_api/v1/rl/instances", json={
        "challenge": "nonexistent/nope",
    })
    data = response.json()
    assert not data["success"]
    assert "not found" in data["error"].lower()
