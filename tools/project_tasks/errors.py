"""Stable, redacted command errors; never infer whether work was executed."""
import argparse
from contextvars import ContextVar
import errno
import http.client
import json
import sqlite3
import socket
import ssl
import sys

PROFILE = "q4t-project-error-v1"
# code: category, fixed public message, operator action. No exception text is
# interpolated, including exception class names supplied by third-party code.
CATALOG = {
    "invalid_arguments": ("input", "Invalid or inapplicable command arguments.", "correct_arguments"),
    "invalid_profile": ("input", "Invalid private client profile.", "check_profile"),
    "unsafe_path": ("input", "Path or private file permissions are not accepted.", "check_private_paths"),
    "not_found": ("storage", "A required local object is missing.", "inspect_storage"),
    "already_exists": ("conflict", "The destination already exists.", "inspect_destination"),
    "busy": ("conflict", "A task or store is in use.", "inspect_after_owner_exits"),
    "task_capacity": ("capacity", "The active task capacity is exhausted.", "review_task_retention"),
    "key_capacity": ("capacity", "The permanent key capacity is exhausted.", "review_capacity_policy"),
    "archive_capacity": ("capacity", "The archive byte capacity is exhausted.", "review_task_retention"),
    "snapshot_capacity": ("capacity", "The snapshot capacity is exhausted.", "review_snapshot_budget"),
    "task_budget": ("capacity", "A durable task or migration budget is exhausted.", "inspect_task"),
    "storage_full": ("storage", "Storage space or transaction headroom is insufficient.", "inspect_storage"),
    "storage_permission": ("storage", "Storage access was denied or is read-only.", "inspect_storage"),
    "storage_error": ("storage", "A storage operation failed.", "inspect_storage"),
    "database_error": ("storage", "A database operation failed.", "preserve_and_inspect"),
    "integrity_error": ("integrity", "Saved data failed integrity validation.", "preserve_and_inspect"),
    "contract_violation": ("contract", "A required contract could not be verified.", "preserve_and_inspect"),
    "key_conflict": ("conflict", "The idempotency key belongs to a different task contract.", "inspect_task"),
    "binding_changed": ("conflict", "The saved client binding does not match.", "review_binding_migration"),
    "runtime_changed": ("conflict", "The saved task runtime requires explicit migration.", "review_runtime_migration"),
    "runtime_unsupported": ("contract", "The saved runtime has no registered migration.", "review_runtime_adapter"),
    "plan_changed": ("conflict", "The plan, digest or destination does not match.", "review_plan"),
    "plan_stale": ("conflict", "The task or source has changed since planning.", "inspect_then_replan"),
    "snapshot_stale": ("conflict", "The snapshot no longer matches the source.", "preserve_then_new_snapshot"),
    "retention_pending": ("recovery", "A retention operation requires explicit completion.", "inspect_retention_then_finish"),
    "store_retired": ("recovery", "The source store is fenced for handoff.", "inspect_handoff_plan"),
    "task_not_terminal": ("conflict", "Only terminal tasks can be archived.", "inspect_task"),
    "retention_period": ("conflict", "The minimum payload retention period has not elapsed.", "check_retention_period"),
    "policy_conflict": ("conflict", "The existing retention policy is immutable.", "review_capacity_policy"),
    "resolution_conflict": ("conflict", "The decision does not match the current task state.", "inspect_then_resolve"),
    "remote_auth": ("remote", "The service rejected authentication or access.", "inspect_task_and_credentials"),
    "remote_capacity": ("remote", "The service rejected admission or is unavailable.", "inspect_task_and_service"),
    "remote_error": ("remote", "The service returned an error.", "inspect_task_and_service"),
    "transport_error": ("transport", "The service transport failed.", "inspect_task_and_connection"),
    "tls_error": ("transport", "TLS verification or negotiation failed.", "check_tls_and_task"),
    "interrupted": ("interruption", "The command was interrupted.", "inspect_before_any_resume"),
    "internal_error": ("internal", "An unexpected command failure occurred.", "preserve_and_inspect"),
}
OPERATIONS = frozenset(("init", "submit", "inspect", "resume", "resolve",
    "plan-migration", "apply-migration", "plan-trust", "apply-trust", "enable-retention", "retention-status",
    "finish-retention", "plan-archive", "apply-archive", "plan-prune",
    "apply-prune", "inspect-archive", "backup", "verify", "plan-restore", "apply-restore"))
OPERATION = ContextVar("project_task_operation", default=None)


class ContractError(ValueError):
    def __init__(self, message, code="contract_violation"):
        if code not in CATALOG:
            raise RuntimeError("unregistered public error code")
        self.code = code
        super().__init__(message)


class Parser(argparse.ArgumentParser):
    def __init__(self, **options):
        super().__init__(allow_abbrev=False, **options)

    def error(self, message):
        # argparse's message may include an entire raw argument or profile path.
        raise ContractError("invalid command arguments", "invalid_arguments")

    def parse_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        seen = set()
        for item in args:
            option = self._option_string_actions.get(item.split("=", 1)[0])
            if option is not None:
                if option.dest in seen:
                    self.error("duplicate option")
                seen.add(option.dest)
        value = super().parse_args(args, namespace)
        OPERATION.set(value.action if value.action in OPERATIONS else None)
        return value


def options(args, required, optional=()):
    """Reject ignored options before profile reads, directory creation or I/O."""
    supplied = {key for key, value in vars(args).items()
                if key != "action" and value is not None}
    if not set(required) <= supplied <= set(required) | set(optional):
        raise ContractError("required/inapplicable command options", "invalid_arguments")


def classify(error):
    # Import after engine initialization; never use arbitrary exception.code or
    # string matching to classify local/remote failures.
    from tools.project_tasks.engine import chat
    if isinstance(error, ContractError):
        return error.code, "ValueError", None
    if isinstance(error, KeyboardInterrupt):
        return "interrupted", "KeyboardInterrupt", None
    if isinstance(error, chat.RemoteError):
        status = error.status if type(error.status) is int and 100 <= error.status <= 599 else None
        code = ("remote_auth" if status in (401, 403) else
                "remote_capacity" if status in (429, 503) else "remote_error")
        return code, "RemoteError", status
    if isinstance(error, ssl.SSLError):
        return "tls_error", "OSError", None
    if isinstance(error, (ConnectionError, TimeoutError, socket.gaierror, http.client.HTTPException)):
        return "transport_error", "OSError", None
    if isinstance(error, sqlite3.Error):
        primary = getattr(error, "sqlite_errorcode", 0) & 0xff
        code = {sqlite3.SQLITE_BUSY: "busy", sqlite3.SQLITE_LOCKED: "busy",
                sqlite3.SQLITE_FULL: "storage_full", sqlite3.SQLITE_READONLY: "storage_permission",
                sqlite3.SQLITE_PERM: "storage_permission", sqlite3.SQLITE_CORRUPT: "integrity_error",
                sqlite3.SQLITE_NOTADB: "integrity_error"}.get(primary, "database_error")
        return code, "DatabaseError", None
    if isinstance(error, OSError):
        code = {errno.EAGAIN: "busy", errno.EWOULDBLOCK: "busy",
                errno.ENOENT: "not_found", errno.EEXIST: "already_exists",
                errno.ENOSPC: "storage_full", errno.EDQUOT: "storage_full",
                errno.EACCES: "storage_permission", errno.EPERM: "storage_permission",
                errno.EROFS: "storage_permission", errno.EINTR: "interrupted"}.get(error.errno, "storage_error")
        return code, "OSError", None
    if isinstance(error, ValueError):
        return "contract_violation", "ValueError", None
    return "internal_error", "Exception", None


def public(error):
    code, legacy, status = classify(error)
    category, message, action = CATALOG[code]
    return dict(profile=PROFILE, error=legacy, code=code, category=category,
                message=message, next_action=action, operation=OPERATION.get(),
                http_status=status, automatic_retry=False)


def run(main):
    token = OPERATION.set(None)
    try:
        return main() or 0
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps(public(error), ensure_ascii=False), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        OPERATION.reset(token)
