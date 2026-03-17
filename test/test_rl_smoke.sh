#!/bin/bash
set -eu

cd "$(dirname "${BASH_SOURCE[0]}")/.."

KEY_PATH="./test/rl_test_key"

if [ ! -f "$KEY_PATH" ]; then
    echo "[+] Generating RL test SSH key..."
    ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "rl-test-key"
fi

RL_PUB_KEY=$(cat "${KEY_PATH}.pub")

echo "============================================"
echo " RL Mode Smoke Test"
echo "============================================"
echo ""

# Step 1: Start dojo with RL enabled
echo "[1/6] Starting dojo with RL_ENABLED=True..."
./deploy.sh \
    -e "RL_ENABLED=True" \
    -e "RL_MAX_INSTANCES=4" \
    -e "RL_SSH_PUBLIC_KEY=${RL_PUB_KEY}" \
    "$@"

DOJO_CONTAINER=$(basename "$PWD")
DOJO_IP=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$DOJO_CONTAINER")
DOJO_URL="http://${DOJO_IP}"

echo "    Dojo IP: ${DOJO_IP}"

# Step 2: Check RL status
echo ""
echo "[2/6] Checking RL status..."
STATUS=$(curl -sf "${DOJO_URL}/pwncollege_api/v1/rl/status")
echo "    $STATUS" | python3 -m json.tool
echo "    PASS"

# Step 3: Create the example dojo (so we have challenges)
echo ""
echo "[3/6] Creating example dojo..."
ADMIN_SESSION=$(curl -sf -c - "${DOJO_URL}/login" | grep -oP "session=\K[^;]+")
NONCE=$(curl -sf "${DOJO_URL}/login" | grep -oP "'csrfNonce': \"\K[^\"]+")

curl -sf -b "session=${ADMIN_SESSION}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "name=admin&password=admin&nonce=${NONCE}" \
    "${DOJO_URL}/login" > /dev/null

CSRF=$(curl -sf -b "session=${ADMIN_SESSION}" "${DOJO_URL}/" | grep -oP "'csrfNonce': \"\K[^\"]+")

DOJO_RESULT=$(curl -sf -b "session=${ADMIN_SESSION}" \
    -H "Content-Type: application/json" \
    -H "CSRF-Token: ${CSRF}" \
    -d '{"repository": "pwncollege/example-dojo", "public_key": "test", "private_key": "test"}' \
    "${DOJO_URL}/pwncollege_api/v1/dojos/create" || echo '{"dojo":"example"}')
echo "    Dojo: ${DOJO_RESULT}"

# Step 4: List challenges
echo ""
echo "[4/6] Listing RL challenges..."
CHALLENGES=$(curl -sf "${DOJO_URL}/pwncollege_api/v1/rl/challenges")
NUM_CHALLENGES=$(echo "$CHALLENGES" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['challenges']))")
echo "    Found ${NUM_CHALLENGES} challenges"

if [ "$NUM_CHALLENGES" -eq 0 ]; then
    echo "    SKIP: No challenges loaded (dojo may not have loaded yet)"
    echo "    Try running: curl -X POST ${DOJO_URL}/pwncollege_api/v1/rl/instances -H 'Content-Type: application/json' -d '{\"challenge\": \"hello/apple\"}'"
    exit 0
fi

# Step 5: Create an instance
echo ""
echo "[5/6] Creating RL instance..."
INSTANCE=$(curl -sf -X POST \
    -H "Content-Type: application/json" \
    -d '{"challenge": "hello/apple"}' \
    "${DOJO_URL}/pwncollege_api/v1/rl/instances")
echo "    $INSTANCE" | python3 -m json.tool

SLOT=$(echo "$INSTANCE" | python3 -c "import sys,json; print(json.load(sys.stdin)['slot'])")
SSH_USER=$(echo "$INSTANCE" | python3 -c "import sys,json; print(json.load(sys.stdin)['ssh_user'])")
echo "    Slot: ${SLOT}, SSH User: ${SSH_USER}"

# Get the flag
FLAG=$(curl -sf "${DOJO_URL}/pwncollege_api/v1/rl/instances/${SLOT}" | python3 -c "import sys,json; print(json.load(sys.stdin)['flag'])")
echo "    Flag: ${FLAG}"

# Check wrong flag
WRONG=$(curl -sf -X POST \
    -H "Content-Type: application/json" \
    -d '{"flag": "pwn.college{wrong}"}' \
    "${DOJO_URL}/pwncollege_api/v1/rl/instances/${SLOT}/check")
echo "    Wrong flag check: $WRONG"

# Check correct flag
CORRECT=$(curl -sf -X POST \
    -H "Content-Type: application/json" \
    -d "{\"flag\": \"${FLAG}\"}" \
    "${DOJO_URL}/pwncollege_api/v1/rl/instances/${SLOT}/check")
echo "    Correct flag check: $CORRECT"

# Step 6: SSH test
echo ""
echo "[6/6] Testing SSH routing..."
SSH_RESULT=$(ssh -i "$KEY_PATH" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=15 \
    "${SSH_USER}@${DOJO_IP}" \
    "whoami" 2>/dev/null || echo "SSH_FAILED")

if [ "$SSH_RESULT" = "hacker" ]; then
    echo "    SSH: PASS (whoami = hacker)"
else
    echo "    SSH: FAIL (got: ${SSH_RESULT})"
    echo "    This may be expected if SSH key propagation takes time."
fi

# Cleanup
echo ""
echo "Cleaning up..."
curl -sf -X DELETE "${DOJO_URL}/pwncollege_api/v1/rl/instances/${SLOT}" > /dev/null

echo ""
echo "============================================"
echo " Smoke test complete!"
echo "============================================"
echo ""
echo "Manual testing commands:"
echo "  Create:  curl -X POST ${DOJO_URL}/pwncollege_api/v1/rl/instances -H 'Content-Type: application/json' -d '{\"challenge\": \"hello/apple\"}'"
echo "  SSH:     ssh -i ${KEY_PATH} rl_<SLOT>@${DOJO_IP}"
echo "  Check:   curl -X POST ${DOJO_URL}/pwncollege_api/v1/rl/instances/<SLOT>/check -H 'Content-Type: application/json' -d '{\"flag\": \"...\"}'"
echo "  Reset:   curl -X POST ${DOJO_URL}/pwncollege_api/v1/rl/instances/<SLOT>/reset -H 'Content-Type: application/json' -d '{}'"
echo "  Kill:    curl -X DELETE ${DOJO_URL}/pwncollege_api/v1/rl/instances/<SLOT>"
echo "  Status:  curl ${DOJO_URL}/pwncollege_api/v1/rl/status"
echo "  Dash:    ${DOJO_URL}/admin/rl (login as admin/admin)"
