# Licensing and attribution review

No blanket open-source license is assigned to the recovered Mars Hotel source. Publication provides the code for preservation, review and recovery; it does not grant rights beyond those held by the respective copyright owners. The Pirates BBS 1.9 / Eagles heritage is identified as provenance and must not be read as permission to relicense inherited work.

The public tree contains source, configuration, text artwork and tests. It excludes registered LORD files and keys, Zork story data, TradeWars game data, production databases, extracted container files and the private deployed-door image.

Third-party runtime components keep their own notices:

| Component | License / handling |
| --- | --- |
| Python | PSF license |
| OpenSSH | BSD-style and component notices supplied by Debian |
| libsodium | ISC |
| python-chess 1.11.2 | GPL-3.0-or-later |
| Stockfish 19 | GPL-3.0; official license, authors and full corresponding source are copied into the built image |
| ngIRCd | GPL-2.0-or-later |
| tini | MIT |
| bubblewrap | LGPL-2.0-or-later |
| dosemu2 | GPL-2.0-or-later |
| FDPP | GPL-3.0-or-later |
| Frotz | GPL-2.0-or-later |

Base distribution packages carry their notices in the resulting images. Operators distributing images must preserve those notices and meet corresponding-source requirements.

Zork, LORD and TradeWars content must be obtained and licensed by each operator. The repository only supplies recovered integration code and generic wrapper/configuration files. Do not publish game archives, registration files or keys from an installed system.

The original project did not contain a project-wide license file. A future license grant should be made only by parties able to establish ownership of the recovered first-party and inherited material.
