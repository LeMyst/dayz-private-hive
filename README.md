# dayz-private-hive

A private **hive** (central server) for **DayZ Server 1.29**, written in pure Python with no
dependencies.

DayZ servers can run in "online" mode: the mission calls `Hive.InitOnline()` and the server
asks a central HTTP service for its economy data and for the players' characters. This is how
the official servers work, and it is what makes a character follow the player from one server
to another. That service is not public. This project is a reimplementation of it that you run
yourself, so that your own servers share one character pool.

What you get:

- **Characters shared across your servers** (position, inventory, health), stored on disk by the hive.
- **Login countdowns like the official servers**: normal login, penalty after an unclean
  disconnect, longer wait after a server hop.
- **Server hopping handling**: hoppers are placed on a `<hop>` spawn point; a player arriving from
  another map is placed on a `<travel>` spawn point.
- **Several maps on one hive**: Chernarus, Livonia and Sakhal servers can share the same hive and
  the same characters.
- **Central economy data served by the hive**: the five `db/*.xml` files of each mission
  (types, events, globals, economy, messages).

## Requirements

- Python 3.9 or newer, on Windows or Linux.
- DayZ Server **1.29**. The protocol was validated against this version; other versions may work
  but were not tested.

## Quick start

The hive runs next to your DayZ server, or on any machine the server can reach over HTTP.

**1. Create an online mission.** Copy the vanilla mission and switch it to online mode:

```
mpmissions/dayzOffline.chernarusplus  ->  mpmissions/dayzOnline.chernarusplus
```

In `mpmissions/dayzOnline.chernarusplus/init.c`, replace

```c
ce.InitOffline();
```

with

```c
ce.InitOnline( "chernarusplus", "http://127.0.0.1:8080/" );
```

The first argument is the **setup name**: it must be the part of the mission folder name after the
last dot. The URL **must end with `/`**.

**2. Edit `serverDZ.cfg`:**

```
template = "dayzOnline.chernarusplus";
shardId  = "123abc";   // 3 or 6 alphanumeric characters, identical on every server sharing characters
instanceId = 1;        // different on every server
```

**3. Start the hive, then the server.** From the DayZ server folder:

```bash
python /path/to/dayz-private-hive/hive.py
```

(or with Docker, see [below](#docker))

The hive finds every `mpmissions/dayzOnline.*` folder by itself and prints the exact
`InitOnline` line to paste for each one. Then start `DayZServer` as usual. It logs
`[CE][Hive] :: Init sequence finished.` when everything is in place.

**Stopping**: stop the DayZ server first, then the hive. The server sends its last character saves
while shutting down. The hive itself can be restarted at any time without touching the servers.

## Configuration

All settings are in [`config.py`](config.py), each with a comment. Edit it and start the hive
without arguments. Any setting can also be set by an environment variable named
`HIVE_<SETTING>` (`HIVE_PORT=9000`, `HIVE_SHARDS=123abc,456def`, `HIVE_LOGIN_TIMES=false`),
or overridden on the command line. The command line wins over the environment, which wins
over the file.

| `config.py` | Environment | Command line | Default | Meaning |
|---|---|---|---|---|
| `MISSIONS` | `HIVE_MISSIONS` | `--mission PATH` (repeatable) | every `mpmissions/dayzOnline.*` | Mission folders to serve, one per map. |
| `STORE` | `HIVE_STORE` | `--store DIR` | `hive_store` | Where characters and sessions are saved. |
| `HOST` | `HIVE_HOST` | `--host HOST` | `127.0.0.1` | Listen address. `0.0.0.0` accepts servers from other machines. |
| `PORT` | `HIVE_PORT` | `--port PORT` | `8080` | Listen port. |
| `CHARACTER_SCOPE` | `HIVE_CHARACTER_SCOPE` | `--character-scope global\|world` | `global` | `global`: one survivor per player for all maps. `world`: one per map. |
| `LOGIN_TIMES` | `HIVE_LOGIN_TIMES` | `--login-times` / `--no-login-times` | `True` | Login, penalty and hopping countdowns. |
| `HOP_WINDOW` | `HIVE_HOP_WINDOW` | `--hop-window SECONDS` | `1800` | A server change within this window counts as a hop. |
| `HOP_RELOCATE` | `HIVE_HOP_RELOCATE` | `--hop-relocate` / `--no-hop-relocate` | `True` | Move a hopping player to a `<hop>` spawn point. |
| `SECRET_PATH` | `HIVE_SECRET_PATH` | `--secret-path PATH` | empty | Answer only under `http://host:port/PATH/`. See [Security](#security). |
| `REQUIRE_TOKEN` | `HIVE_REQUIRE_TOKEN` | `--require-token` / `--no-require-token` | `True` | Refuse requests with a session token this hive did not issue. |
| `SHARDS` | `HIVE_SHARDS` | `--shard ID` (repeatable) | empty | Accept only these `shardId` values. |
| `ALLOWED_IPS` | `HIVE_ALLOWED_IPS` | `--allowed-ip ADDR` (repeatable) | empty | Accept only these addresses or networks. |
| `STATUS_PASSWORD` | `HIVE_STATUS_PASSWORD` | `--status-password PASSWORD` | empty | Enable the status page for user `admin`. |

Lists are Python lists in the file, comma-separated in the environment, repeated flags on the
command line. Booleans are `True`/`False` in the file and `true`/`false` in the environment.

The login countdown durations themselves (15 s, 20 s penalty, 60 s hop by default) come from
the DayZ server, not from the hive.

## Docker

A container image is built from [`Dockerfile`](Dockerfile) on every push to `main` and
published as `ghcr.io/lemyst/dayz-private-hive:latest`. To run it, copy
[`docker-compose.yaml`](docker-compose.yaml) next to your DayZ server folder (where
`mpmissions/` is) and start it:

```bash
docker compose up -d
```

The compose file mounts:

| Path in the container | Content |
|---|---|
| `/data/mpmissions` | your `mpmissions/` folder, read-only; every `dayzOnline.*` mission is served |
| `/data/hive_store` | characters and sessions, in a named volume |

Settings go in the `environment` block as `HIVE_<SETTING>` variables; the compose file has
commented examples. Inside the container the hive always listens on `0.0.0.0:8080`. The compose
file publishes it on `127.0.0.1:8080` only; change the `ports` entry to `"8080:8080"` for DayZ
servers on other machines, and put a firewall in front of it.

The container runs as uid 1000, so the store must be writable by that user. A fresh named
volume is set up correctly by the image; a bind-mounted folder, or a volume created by an
older image, needs a one-time `chown -R 1000:1000` (the hive refuses to start otherwise and
says so).

To build the image yourself instead of pulling it:

```bash
docker build -t dayz-private-hive .
```

## Several servers, several maps

- Give every server the **same `shardId`** and a **different `instanceId`**.
- One `dayzOnline.<map>` mission per map. Its setup name is the folder suffix (`chernarusplus`,
  `enoch`, `sakhal`, ...), so `InitOnline( "sakhal", ... )` in the Sakhal mission.
- Servers on other machines: run the hive with `--host 0.0.0.0` (or set `HOST` in `config.py`) and
  use the hive machine's address in `InitOnline`. **The protocol has no authentication**: only
  expose the port to your own servers, with a firewall.

## Security

The DayZ server's HTTP client cannot send credentials, so protection comes from the network and
from what the protocol happens to carry. The layers below are all in `config.py`; the first four
are optional and off until you set them.

| Setting | What it does |
|---|---|
| `SECRET_PATH = "k3Qz9pLm2vXw7bNd"` | The hive answers only under `http://host:8080/k3Qz9pLm2vXw7bNd/`, and `InitOnline` must use that URL. The server appends `init/` and `run/` to whatever URL it is given, so this works as a shared secret that players never see. **The most effective layer.** |
| `REQUIRE_TOKEN = True` (default) | Every request after the handshake carries the session token issued by the hive; unknown tokens are refused. If you delete the store while servers are running, restart them. |
| `SHARDS = ["123abc"]` | Only servers with one of these `shardId` values may register. |
| `ALLOWED_IPS = ["10.4.2.41", "10.4.2.0/24"]` | Only these addresses or networks may talk to the hive, status page included. |

Always on: request bodies are capped at 4 MB, and a character is only loaded, saved or killed if
its id was issued by this hive. Refused requests are logged with the client address.

**Status page.** Set `STATUS_PASSWORD` and open `http://host:8080/` (or the secret path) in a
browser as user `admin`. It shows the served missions, the registered servers with their map and
online player count, the players online, alive characters and deaths, and the last hive events.
Without a password the page does not exist. The password travels in clear over HTTP, so use it
on a trusted network only.

Keep the hive on `127.0.0.1` when the servers run on the same machine. Otherwise put a firewall
in front of it, or a VPN between the machines. The Docker image runs as a non-root user and the
compose file mounts the container read-only.

## What the hive serves, and what it does not

The hive replaces only the "database" part of the central economy: the five files in the
mission's `db/` folder. In online mode the server never reads them from disk.

| File | Read by |
|---|---|
| `db/types.xml`, `db/events.xml`, `db/globals.xml`, `db/economy.xml`, `db/messages.xml` | **the hive** |
| `cfglimitsdefinition.xml`, `cfglimitsdefinitionuser.xml` | both |
| every other `cfg*.xml`, `mapgroup*.xml`, `areaflags.map` | the server, from disk |

The server fetches these five files from the hive **once, at startup**, and the hive encodes
them **once, when it starts**. So after editing any file in `db/`, restart the hive, then the
servers: a running server keeps the data it loaded. The hive can be restarted while the servers
are up; they simply pick up the new data at their next start.

**Why the hive reads `cfglimitsdefinition.xml` and `cfglimitsdefinitionuser.xml`.**
`types.xml` refers to categories, tags, usage flags and value flags **by name**
(`<category name="tools"/>`, `<usage name="Military"/>`, ...). The protocol does not carry
names: each type is sent with a category **index** and three **bitmasks**, whose meaning is
the order of the entries in `cfglimitsdefinition.xml` (first entry = first bit).
`cfglimitsdefinitionuser.xml` defines aliases such as `Town` that expand to several of those
bits. The hive needs both files only to translate the names into those numbers; it never sends
them. The server reads the same two files for its own use, which is why they must be identical
on both sides.

**Why not the other files.** Everything else in the mission describes the map itself: where
loot spawns (`mapgroup*.xml`, `areaflags.map`), where events and players spawn
(`cfgeventspawns.xml`, `cfgeventgroups.xml`, `cfgplayerspawnpoints.xml`), what each item can
contain (`cfgspawnabletypes.xml`, `cfgrandompresets.xml`), weather and environment. The
protocol has no field for any of it: the server loads these from disk in every mode, and the
hive would have nothing to do with them.

## Data

`hive_store/` (or `--store`) contains:

- `characters/<id>.bin`: one character per file, as saved by the server.
- `index.json`: player -> character id, and the session state used for login countdowns.
- `sessions.json`: the servers that registered with the hive, kept across hive restarts.

It contains Steam IDs and characters: do not publish it. Back it up like any player database.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Server log: `Unknown Central Economy setup detected` | The first argument of `InitOnline` does not match a served mission's folder suffix. The hive logs the hash it received. |
| Server log: `Server host init failed` | The hive is not running, or the URL in `InitOnline` is wrong or lacks the trailing `/`. |
| Server starts but there is no loot | The mission has no `db/` folder or its `types.xml` is empty. The hive warns `no types found` at startup. |
| Server log: `No Shutdown message present` | Harmless: `db/messages.xml` has no shutdown message. |
| Client kicked with `Game restart required` | BattlEye on the client side. Launch the game through `DayZ_BE.exe`, not `DayZ_x64.exe`. |
| Hopping is not detected after a hive restart | `sessions.json` is missing from the store: the servers' tokens were lost. Restart the servers. |

## Notes

This repository contains no files from Bohemia Interactive. The hive protocol was reimplemented
independently; the DayZ server, its data and missions are not redistributed here.

Licensed under the [Elastic License 2.0](LICENSE).
