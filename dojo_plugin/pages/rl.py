from flask import Blueprint, render_template
from CTFd.utils.decorators import admins_only

from ..config import RL_ENABLED

rl = Blueprint("pwncollege_rl", __name__)


@rl.route("/admin/rl")
@admins_only
def dashboard():
    if not RL_ENABLED:
        return "RL mode is not enabled", 404
    return render_template("rl_dashboard.html")
