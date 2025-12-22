from functools import wraps
from flask import current_app, request

CARE_ADMIN = "CARE_ADMIN"
NURSE = "NURSE"
FAMILY = "FAMILY"


def get_current_role(req) -> str:
    default_role = NURSE
    if not current_app.debug:
        return default_role
    # Only in debug allow overrides
    role = req.headers.get("X-ROLE") or req.args.get("role") or default_role
    return role.strip().upper() if isinstance(role, str) else default_role


def require_roles(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method != "POST":
                return fn(*args, **kwargs)
            role = get_current_role(request)
            if role not in roles:
                return "Forbidden", 403
            return fn(*args, **kwargs)

        return wrapper

    return decorator
