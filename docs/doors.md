# Optional DOS/native doors

The recovered doord implementation supports Zork I, LORD 4.02 and TradeWars 2002 3.09. Production enables only zork,lord. TradeWars code and templates exist but active production play was not verified. Do not equate the Python gtw game with TradeWars.

Dockerfile.door reconstructs the runtime from Ubuntu packages and the exact recovered dosemu2/fdpp versions. No proprietary game or registration files are in Git. The doors Compose profile requires an operator-supplied licensed-doors directory mounted read-only as /opt/mars-doors/templates. Its layout must match the recovered templates: zork/zork1-r119-s880429.z3; lord/home/.dosemu/drive_c/doors/lord; tradewars/home/.dosemu/drive_c/doors/tw, including matching DOS configuration and the wrapper scripts provided in assets/doors. The current recovered authority seeds and initializes both DOS worlds on startup, even when only one is enabled; all required templates must therefore be installed.

Use docker compose --profile doors build door, then docker compose --profile doors up -d after reviewing licenses and supplying complete templates. MARS_ENABLED_DOORS defaults to zork,lord; MARS_LORD_NODES defaults to 4. An optional LORD registration file must be a private regular file owned by root or UID 21011. Add it through a private runtime mount and the --lord-registration-file argument; never put it in Git or image layers.

The recovered seccomp policy is preserved in security/door-seccomp.json. The runtime also uses Landlock and unprivileged isolation; do not replace these with privileged execution if host kernel support is missing.

The exact historical dosemu2 and FDPP versions are no longer available from the configured PPA, so a clean `Dockerfile.door` build currently stops at package resolution. A private export of the deployed image was preserved during recovery. An authorized operator can load that archive locally and set `MARS_DOOR_IMAGE` to its local tag, then start with `--no-build`; the archive and licensed templates must remain private. Do not substitute a different emulator version and describe it as the recovered runtime without separate compatibility tests.
