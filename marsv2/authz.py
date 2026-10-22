"""Historical roles mapped to narrowly scoped V2 capabilities.

The names intentionally preserve the Eagles/Pirate administration vocabulary,
but authorization is performed with named capabilities rather than the legacy
integer level or permission bit mask.
"""

from __future__ import annotations


ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "Basic-1": frozenset(),
    "AccountMgr": frozenset({
        "account.view", "account.manage", "account.password_reset",
        "account.delete",
    }),
    "BoardMgr": frozenset({"board.manage", "board.moderate"}),
    "Music": frozenset({"music.play"}),
    "Sysop": frozenset({
        "account.view", "account.manage", "account.password_reset",
        "account.delete", "account.roles",
        "board.manage", "board.moderate", "session.manage",
        "audit.read", "system.health", "federation.manage",
        "music.play", "music.manage",
    }),
}

ASSIGNABLE_ROLES = tuple(ROLE_CAPABILITIES)
ROLE_DESCRIPTIONS: dict[str, str] = {
    "Basic-1": "Standard member. No administrative authority.",
    "AccountMgr": (
        "Manage ordinary accounts: status, temporary-password resets, and deletion. "
        "Cannot assign roles or administer a Sysop."
    ),
    "BoardMgr": (
        "Create, rename, lock, archive, restore, and moderate message boards."
    ),
    "Music": (
        "Use the authenticated MarsAmp catalog and audio player."
    ),
    "Sysop": (
        "Full BBS administrator: accounts and roles, boards, sessions, audit log, "
        "application health, BBS identity, trusted mail links, and MarsAmp."
    ),
}
ADMIN_CONSOLE_CAPABILITIES = frozenset({
    "account.view", "account.manage", "account.password_reset", "account.delete",
    "account.roles", "board.manage", "board.moderate", "session.manage",
    "audit.read", "system.health", "federation.manage", "music.manage",
})


def capabilities_for(roles: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    result: set[str] = set()
    for role in roles:
        result.update(ROLE_CAPABILITIES.get(role, ()))
    return sorted(result)


def has(identity: dict[str, object], capability: str) -> bool:
    values = identity.get("capabilities", [])
    return isinstance(values, list) and capability in values
