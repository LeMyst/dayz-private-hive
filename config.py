"""Hive settings. Edit this file, then run: python hive.py

Every setting can also be set by an environment variable named HIVE_<SETTING>
(lists as comma-separated values, booleans as true/false), or overridden on
the command line (python hive.py --help).
"""

import os


def _env(name, default):
    """HIVE_<name> from the environment, converted to the type of default."""
    raw = os.environ.get("HIVE_" + name)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, list):
        return [v.strip() for v in raw.split(",") if v.strip()]
    return raw


# Mission folders to serve, one per map, e.g. ["mpmissions/dayzOnline.chernarusplus"].
# Empty list = every mpmissions/dayzOnline.* folder found in the current directory.
# The setup name passed to InitOnline() in init.c is the part after the last dot
# of the folder name ("dayzOnline.chernarusplus" -> "chernarusplus").
MISSIONS = _env("MISSIONS", [])

# Folder where characters and server sessions are saved.
STORE = _env("STORE", "hive_store")

# Address and port the hive listens on. The DayZ server connects to
# http://HOST:PORT/ . Use "0.0.0.0" to accept servers from other machines;
# there is no authentication, so restrict access with a firewall.
HOST = _env("HOST", "127.0.0.1")
PORT = _env("PORT", 8080)

# "global": one survivor per player, shared by all maps of the shard (official behaviour).
# "world":  one survivor per player and per map.
CHARACTER_SCOPE = _env("CHARACTER_SCOPE", "global")

# Login countdowns: normal login, penalty after an unclean disconnect, and
# server hopping. False = players enter immediately.
LOGIN_TIMES = _env("LOGIN_TIMES", True)

# A reconnection on another server within this many seconds counts as a hop.
HOP_WINDOW = _env("HOP_WINDOW", 1800)

# Move a hopping player to a <hop> spawn point instead of where they logged out.
HOP_RELOCATE = _env("HOP_RELOCATE", True)

# ---- Security. Each layer is optional: empty, 0 or False disables it. ----

# Secret path, e.g. "k3Qz9pLm2vXw7bNd". The hive then answers only under
# http://HOST:PORT/k3Qz9pLm2vXw7bNd/ and init.c must use that URL.
SECRET_PATH = _env("SECRET_PATH", "")

# Reject requests whose session token was not issued by this hive.
# Restart the servers if you delete the store while they are running.
REQUIRE_TOKEN = _env("REQUIRE_TOKEN", True)

# Only accept servers whose shardId is in this list, e.g. ["123abc"].
SHARDS = _env("SHARDS", [])

# Only accept requests from these addresses or networks,
# e.g. ["10.4.2.41", "10.4.2.0/24"].
ALLOWED_IPS = _env("ALLOWED_IPS", [])

# Password of the status page at http://HOST:PORT/ (user "admin").
# Empty = no status page. Plain HTTP: use it on a trusted network only.
STATUS_PASSWORD = _env("STATUS_PASSWORD", "")
