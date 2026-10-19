# Optional DOS/native doors

The deployed Mars Hotel offered Zork I and registered LORD 4.02a. TradeWars was not installed or offered. A dormant TradeWars branch and template residue were recovered from the door image because the original build seeded it unconditionally; they are retained in source provenance but are not part of the public deployment or its requirements. Do not equate the Python `gtw` game with TradeWars.

`Dockerfile.door` reconstructs the runtime from Ubuntu packages and the exact recovered dosemu2, FDPP, libfdpp and comcom64 versions. It also installs the unmodified public `vendor/lord/lord402.zip` archive and the verified public registration in `vendor/lord/registration.txt`. On a fresh door volume, the authority creates the LORD world and applies that registration once. The registration file is read-only in the image and LORD's setup transcript is never persisted because it can echo the numbers.

Zork's story file is not included. Place a legally obtained release 119 story at `licensed-doors/zork/zork1-r119-s880429.z3`. Compose mounts only that directory into the built-in template tree, so it does not hide the bundled LORD template.

Use `docker compose --profile doors build door`, then `docker compose --profile doors up -d` after supplying Zork. `MARS_ENABLED_DOORS` defaults to `zork,lord`; `MARS_LORD_NODES` defaults to `4`. Door startup now seeds and initializes only explicitly enabled games. This preserves production behavior while removing the recovered build's unintended TradeWars dependency.

The recovered seccomp policy is preserved in security/door-seccomp.json. The runtime also uses Landlock and unprivileged isolation; do not replace these with privileged execution if host kernel support is missing.

The exact dosemu2 build was published on October 16, 2026 and superseded in the rolling PPA on October 17. Launchpad removed it from the APT pool at 00:53 UTC on October 19 (20:53 EDT on October 18), but retains the exact amd64 artifact on build record 33849543. `Dockerfile.door` downloads that artifact with SHA-256 `3c9cbc002c9f90a9afdaf582e7a73a530faaa233b3b8918a1caaf20b7676dbb3`; its installed emulator binary is byte-identical to production. The PPA still supplies the pinned FDPP, libfdpp and comcom64 builds. A private export of the complete deployed image remains preserved as a fallback. Do not substitute a newer emulator version and describe it as the recovered runtime without separate compatibility tests.
