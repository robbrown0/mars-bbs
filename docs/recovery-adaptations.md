# Verified recovery adaptations

Original hashes remain in `source-provenance.json`; private captures preserve the unchanged deployed files. Build definitions, tests and documentation are reconstruction work.

- `informationd.py` reads its HTTP User-Agent from `MARS_INFORMATION_USER_AGENT`. The recovered default embedded an operator email address.
- the gateway writes the configured public hostname to a root-owned runtime file used by the welcome screen; the source artwork uses an RFC 5737 documentation address
- the supervisor forwards only the existing download host and port settings into authenticated sessions
- recovered test fixtures now supply the deployed identity fields, activity state and IRC MOTD framing expected by the selected source
- the build downloads the official Stockfish 19 x86-64 release archive by HTTPS and verifies SHA-256 `9defc0d4e55d49c65a6d042f3e571a39fcea499ade6dbe741b53b8c65e03611f`; its executable hash `0f83d24cc46d2c66c60f16001af5444873bc112b7d028594513426894c12da19` exactly matches the deployed game container
- Debian OpenSSH 10.0p1 replaces the unavailable custom OpenSSH 10.6p1 build and passed forced-command, authentication and privilege-boundary tests
- the door image now builds its LORD template from the exact public `lord402.zip` recovered from the original build source, then applies the recovered and independently validated public registration on first initialization
- door startup seeds and initializes only explicitly enabled games; this removes the original image's unintended TradeWars template requirement while preserving the production `zork,lord` service set

No historical variant was merged without matching source or behavior evidence.
