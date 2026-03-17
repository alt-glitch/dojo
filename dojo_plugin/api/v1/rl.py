import logging

from flask import request
from flask_restx import Namespace, Resource
from CTFd.plugins import bypass_csrf_protection

from ...config import RL_ENABLED, RL_MAX_INSTANCES
from ...models import DojoChallenges, DojoModules
from ...utils.rl import rl_manager

logger = logging.getLogger(__name__)

rl_namespace = Namespace("rl", description="RL environment endpoints")


def rl_enabled_or_404():
    if not RL_ENABLED:
        return {"success": False, "error": "RL mode is not enabled"}, 404
    return None


@rl_namespace.route("/instances")
class RLInstances(Resource):
    @bypass_csrf_protection
    def get(self):
        if err := rl_enabled_or_404():
            return err
        instances = rl_manager.list_instances()
        return {"success": True, "instances": instances}

    @bypass_csrf_protection
    def post(self):
        if err := rl_enabled_or_404():
            return err

        data = request.get_json()
        challenge_ref = data.get("challenge")
        variant = data.get("variant")

        if not challenge_ref or "/" not in challenge_ref:
            return {"success": False, "error": "challenge must be in format 'module_id/challenge_id'"}

        module_id, challenge_id = challenge_ref.split("/", 1)

        dojo_challenge = (
            DojoChallenges.query
            .filter_by(id=challenge_id)
            .join(DojoModules.query.filter_by(id=module_id).subquery())
            .first()
        )
        if not dojo_challenge:
            return {"success": False, "error": f"Challenge not found: {challenge_ref}"}

        try:
            result = rl_manager.create_instance(dojo_challenge, variant=variant)
            return {"success": True, **result}
        except RuntimeError as e:
            logger.exception(f"Failed to create RL instance for {challenge_ref}")
            return {"success": False, "error": str(e)}


@rl_namespace.route("/instances/<int:slot>")
class RLInstance(Resource):
    @bypass_csrf_protection
    def get(self, slot):
        if err := rl_enabled_or_404():
            return err

        info = rl_manager.get_instance(slot)
        if not info:
            return {"success": False, "error": f"No instance at slot {slot}"}

        info["ssh_user"] = f"rl_{slot}"
        return {"success": True, **info}

    @bypass_csrf_protection
    def delete(self, slot):
        if err := rl_enabled_or_404():
            return err

        rl_manager.destroy_instance(slot)
        return {"success": True}


@rl_namespace.route("/instances/<int:slot>/check")
class RLCheck(Resource):
    @bypass_csrf_protection
    def post(self, slot):
        if err := rl_enabled_or_404():
            return err

        data = request.get_json()
        submitted_flag = data.get("flag", "")
        correct = rl_manager.check_flag(slot, submitted_flag)
        return {"success": True, "correct": correct}


@rl_namespace.route("/instances/<int:slot>/reset")
class RLReset(Resource):
    @bypass_csrf_protection
    def post(self, slot):
        if err := rl_enabled_or_404():
            return err

        data = request.get_json() or {}
        challenge_ref = data.get("challenge")
        dojo_challenge = None

        if challenge_ref:
            if "/" not in challenge_ref:
                return {"success": False, "error": "challenge must be in format 'module_id/challenge_id'"}
            module_id, challenge_id = challenge_ref.split("/", 1)
            dojo_challenge = (
                DojoChallenges.query
                .filter_by(id=challenge_id)
                .join(DojoModules.query.filter_by(id=module_id).subquery())
                .first()
            )
            if not dojo_challenge:
                return {"success": False, "error": f"Challenge not found: {challenge_ref}"}

        try:
            result = rl_manager.reset_instance(slot, dojo_challenge=dojo_challenge)
            return {"success": True, **result}
        except KeyError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.exception(f"Failed to reset RL instance at slot {slot}")
            return {"success": False, "error": str(e)}


@rl_namespace.route("/challenges")
class RLChallenges(Resource):
    @bypass_csrf_protection
    def get(self):
        if err := rl_enabled_or_404():
            return err

        challenges = DojoChallenges.query.all()
        result = []
        for ch in challenges:
            result.append({
                "id": ch.id,
                "name": ch.name,
                "description": ch.description,
                "module_id": ch.module.id if ch.module else None,
                "dojo_id": ch.dojo.reference_id if ch.dojo else None,
            })
        return {"success": True, "challenges": result}


@rl_namespace.route("/status")
class RLStatus(Resource):
    @bypass_csrf_protection
    def get(self):
        if err := rl_enabled_or_404():
            return err

        instances = rl_manager.list_instances()
        return {
            "success": True,
            "enabled": True,
            "max_instances": RL_MAX_INSTANCES,
            "running": len(instances),
            "instances": instances,
        }
