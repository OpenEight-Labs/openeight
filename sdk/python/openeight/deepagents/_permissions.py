"""Copy native ordered filesystem rules without changing their matching semantics."""

from deepagents.middleware.filesystem import FilesystemPermission

from openeight.errors import ConfigurationError


def copy_permissions(value):
    if value is None:
        return None
    failed = False
    result = []
    try:
        for rule in value:
            if (
                type(rule) is not FilesystemPermission
                or set(vars(rule)) != {"operations", "paths", "mode"}
                or type(rule.operations) not in (tuple, list)
                or any(
                    operation not in {"read", "write"} for operation in rule.operations
                )
                or type(rule.paths) not in (tuple, list)
                or any(type(path) is not str for path in rule.paths)
                or rule.mode not in {"allow", "deny", "interrupt"}
            ):
                raise ValueError
            result.append(
                FilesystemPermission(
                    operations=list(rule.operations),
                    paths=list(rule.paths),
                    mode=rule.mode,
                )
            )
    except Exception:
        failed = True
    if failed:
        raise ConfigurationError("invalid_integration_configuration")
    return tuple(result)


def has_native_interrupts(rules, settings):
    from deepagents.graph import _merge_fs_interrupt_on
    from deepagents.middleware._fs_interrupt import _build_interrupt_on_from_permissions
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    failed = False
    result = False
    try:
        merged = _merge_fs_interrupt_on(
            _build_interrupt_on_from_permissions(list(rules or ())), settings
        )
        result = bool(HumanInTheLoopMiddleware(interrupt_on=merged or {}).interrupt_on)
    except Exception:
        failed = True
    if failed:
        raise ConfigurationError("invalid_integration_configuration")
    return result
