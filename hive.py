#!/usr/bin/env python3
"""Private hive (central server) for DayZ Server 1.29.

Answers the five requests a server makes after Hive.InitOnline():

    POST init/process/     handshake, returns a session token
    POST init/data/        central economy data (db/*.xml of the mission)
    POST run/ping/?uid=    player login: character id, login countdown, spawn mode
    POST run/get/          character load
    POST run/process/      SAVE / KILL / EXIT stream

Settings live in config.py; command-line flags override them. Standard library only.
"""

import argparse
import base64
import glob
import hmac
import html
import ipaddress
import json
import os
import random
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config                                                  # noqa: E402
from mission import Mission                                    # noqa: E402
from wire import Reader, Writer, hash37, hive_compress, hive_decompress  # noqa: E402

FLAG_CUSTOM_SETUP = 0x01

# Character status word: any of bits 0x29 set = create a new character.
STATUS_NEW = 0x01
STATUS_EXISTING = 0x00
# Spawn selection bits of the same word (cfgplayerspawnpoints.xml groups).
STATUS_SPAWN_TRAVEL = 0x40      # <travel>: arriving from another map
STATUS_SPAWN_HOP = 0x80         # <hop>: arriving from another server of the same map

# run/process/ opcodes
OP_SAVE, OP_KILL, OP_EXIT, OP_EXITGAME = 0, 1, 2, 3

MAX_BODY = 4 * 1024 * 1024      # largest request body accepted
STATUS_USER = "admin"           # basic auth user of the status page

RECENT = deque(maxlen=40)       # last log lines, shown on the status page


def log(fmt, *args):
    line = fmt % args if args else fmt
    RECENT.append(time.strftime("%Y-%m-%d %H:%M:%S ") + line)
    print("[hive] " + line, flush=True)


class Rejected(Exception):
    """A request refused by a security check; answered with the given HTTP code."""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code


class Store:
    """Characters on disk: index.json (player -> id and session state), one blob per id,
    and sessions.json (server tokens, so the hive can restart without the servers)."""

    # SAVE blob header: uint16 version, int32 playerId, int32 charId.
    OFF_PLAYER_ID = 2
    OFF_CHAR_ID = 6
    HEADER_LEN = 10

    def __init__(self, root, world_scope=False):
        self.root = root
        self.world_scope = world_scope
        self.chars = os.path.join(root, "characters")
        try:
            os.makedirs(self.chars, exist_ok=True)
            probe = os.path.join(root, ".write-test")
            with open(probe, "w"):
                pass
            os.remove(probe)
        except OSError as exc:
            sys.exit("[hive] store %s is not writable: %s\n"
                     "       In Docker the hive runs as uid 1000: chown -R 1000:1000 the volume."
                     % (os.path.abspath(root), exc))
        self.index_path = os.path.join(root, "index.json")
        self.lock = threading.Lock()
        self.index = {"next_id": 1000, "players": {}}
        if os.path.isfile(self.index_path):
            with open(self.index_path) as fh:
                self.index = json.load(fh)

    def load_sessions(self):
        path = os.path.join(self.root, "sessions.json")
        if not os.path.isfile(path):
            return {}
        with open(path) as fh:
            return {int(k): v for k, v in json.load(fh).items()}

    def save_sessions(self, sessions):
        path = os.path.join(self.root, "sessions.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({str(k): v for k, v in sessions.items()}, fh, indent=2)
        os.replace(tmp, path)

    def _flush(self):
        tmp = self.index_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.index, fh, indent=2)
        os.replace(tmp, self.index_path)

    def key(self, uid, shard, world):
        """Index key: one character per shard, or per shard and map."""
        if self.world_scope:
            return "%s|%s|%s" % (shard, world, uid)
        return "%s|%s" % (shard, uid)

    def open_session(self, uid, shard, instance, world):
        """Records a login and returns the entry as it was before it."""
        key = self.key(uid, shard, world)
        with self.lock:
            entry = self.index["players"].get(key)
            if entry is None:
                entry = {"id": self.index["next_id"], "uid": uid, "shard": shard,
                         "world": world, "last_seen": 0, "instance": None,
                         "exited": True, "saved": False, "last_exit": 0, "deaths": 0}
                self.index["next_id"] += 1
                self.index["players"][key] = entry
            previous = dict(entry)
            entry["last_seen"] = int(time.time())
            entry["instance"] = instance
            entry["world"] = world
            entry["exited"] = False
            entry["saved"] = False
            self._flush()
            return previous

    def players(self):
        """Snapshot of every index entry."""
        with self.lock:
            return [dict(e) for e in self.index["players"].values()]

    def _entry_by_id(self, ident):
        for entry in self.index["players"].values():
            if entry["id"] == ident:
                return entry
        return None

    def has_id(self, ident):
        with self.lock:
            return self._entry_by_id(ident) is not None

    def mark_saved(self, ident):
        with self.lock:
            entry = self._entry_by_id(ident)
            if entry is not None and not entry.get("saved"):
                entry["saved"] = True
                self._flush()

    def close_session(self, ident):
        """Clean logout (EXIT received). Returns the uid, or None if unknown."""
        with self.lock:
            entry = self._entry_by_id(ident)
            if entry is None:
                return None
            entry["exited"] = True
            entry["last_exit"] = int(time.time())
            self._flush()
            return entry["uid"]

    def _blob_path(self, pid):
        return os.path.join(self.chars, "%d.bin" % pid)

    def load(self, pid):
        path = self._blob_path(pid)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()

    def save(self, pid, blob):
        with self.lock:
            with open(self._blob_path(pid), "wb") as fh:
                fh.write(blob)

    def kill(self, pid):
        with self.lock:
            entry = self._entry_by_id(pid)
            if entry is not None:
                entry["deaths"] = entry.get("deaths", 0) + 1
                self._flush()
            path = self._blob_path(pid)
            if os.path.isfile(path):
                os.remove(path)
                return True
        return False

    def alive(self, pid):
        return os.path.isfile(self._blob_path(pid))


class Hive:
    """Protocol logic behind the five endpoints."""

    def __init__(self, store, login_times=True, hop_window=1800, hop_relocate=True,
                 require_token=True, shards=()):
        self.store = store
        self.login_times = login_times
        self.hop_window = hop_window
        self.hop_relocate = hop_relocate
        self.require_token = require_token
        self.shards = {s.upper() for s in shards}   # the server sends the shardId upper-cased
        self.missions = {}               # setup hash -> (setup name, compressed body, row counts)
        self.pending_spawn = {}          # char id -> spawn bits decided at ping, applied at get
        self.started = time.time()
        self.sessions = store.load_sessions()   # token -> handshake info, one per server start
        if self.sessions:
            log("%d server session(s) restored: %s", len(self.sessions),
                ", ".join("instance %s" % i.get("instance_id")
                          for _, i in sorted(self.sessions.items())))

    def add_mission(self, setup, mission):
        """Serves a mission under a setup name. Returns the row counts per block."""
        payload, counts = mission.init_data_payload()
        self.missions[hash37(setup)] = (setup, hive_compress(payload), counts)
        return counts

    def session(self, token):
        """Handshake info for a token; rejects unknown tokens when required."""
        info = self.sessions.get(token)
        if info is None and self.require_token:
            raise Rejected(403, "unknown session token")
        return info or {}

    def check_shard(self, shard):
        if self.shards and shard.upper() not in self.shards:
            raise Rejected(403, "shard %r not allowed" % shard)

    def world_name(self, world):
        """Map name for a world hash (hex string), when a served mission matches it."""
        for h, (setup, _, _) in self.missions.items():
            if "%08x" % h == world:
                return setup
        return world or "?"

    # -- init/process/ -----------------------------------------------------

    def init_process(self, body):
        r = Reader(body)
        info = {
            "port": r.i32(),
            "world_hash": r.i32(),
            "instance_id": r.i32(),
            "shard_id": r.small_string(),
            "hostname": r.string() if r.remaining() >= 4 else "",
        }
        self.check_shard(info["shard_id"])
        token = random.getrandbits(31)
        while token in self.sessions:
            token = random.getrandbits(31)
        info["token"] = token
        info["since"] = int(time.time())
        self.sessions[token] = info
        self.store.save_sessions(self.sessions)
        log("init/process/  port=%d instance=%d shard=%r name=%r map=%s",
            info["port"], info["instance_id"], info["shard_id"], info["hostname"],
            self.world_name("%08x" % (info["world_hash"] & 0xFFFFFFFF)))

        w = Writer()
        w.boolean(True)                            # world known
        w.i32(1).i32(0).i32(0)                     # minimum server version
        w.i32(99).i32(99).i32(999999)              # maximum server version
        w.boolean(False)                           # version check disabled
        w.i32(token)
        w.i32(FLAG_CUSTOM_SETUP)
        return w.bytes()

    # -- init/data/ --------------------------------------------------------

    def init_data(self, body):
        """Returns the compressed economy data, or None for an unknown setup."""
        r = Reader(body)
        self.session(r.i32())
        setup_hash = r.i32() & 0xFFFFFFFF
        entry = self.missions.get(setup_hash)
        if entry is None:
            log("init/data/     unknown setup hash %#010x: check the first argument "
                "of InitOnline() against the mission folder names", setup_hash)
            return None
        setup, payload, _ = entry
        log("init/data/     setup=%r -> %d bytes", setup, len(payload))
        return payload

    # -- run/ping/ ---------------------------------------------------------

    def run_ping(self, body, uid):
        r = Reader(body)
        session = self.session(r.i32())
        world_hash = r.i32()
        t_login, t_penalty, t_hopping = r.u16(), r.u16(), r.u16()
        shard = r.small_string() if r.remaining() else session.get("shard_id", "")
        self.check_shard(shard)

        instance = session.get("instance_id")
        world = "%08x" % (world_hash & 0xFFFFFFFF)
        previous = self.store.open_session(uid, shard, instance, world)
        pid = previous["id"]
        has_char = self.store.load(pid) is not None
        status = STATUS_EXISTING if has_char else STATUS_NEW
        travel = previous.get("world") not in (None, world)
        wait, reason, hopping = self.login_time(previous, instance, travel,
                                                t_login, t_penalty, t_hopping)
        spawn = 0
        if travel:
            spawn = STATUS_SPAWN_TRAVEL
        elif hopping and self.hop_relocate:
            spawn = STATUS_SPAWN_HOP
        self.pending_spawn[pid] = spawn
        log("run/ping/      uid=%s shard=%s instance=%s -> id=%d (%s), wait %ds: %s%s",
            uid, shard, instance, pid, "existing" if has_char else "new", wait, reason,
            {STATUS_SPAWN_TRAVEL: ", <travel> spawn", STATUS_SPAWN_HOP: ", <hop> spawn"}
            .get(spawn, ""))
        if instance is None:
            log("               unknown token: instance unknown, hopping not detected")

        w = Writer()
        w.u16(2)
        w.i32(pid)              # player id
        w.i32(pid)              # character id
        w.i32(wait)             # login countdown in seconds
        w.i32(status | spawn)
        return w.bytes()

    def login_time(self, previous, instance, travel, t_login, t_penalty, t_hopping):
        """Picks the login countdown among the three durations offered by the server.
        Returns (seconds, reason, is_hop)."""
        if not self.login_times:
            return 0, "login times disabled", False
        if previous.get("saved") and not previous.get("exited", True):
            return t_penalty, "penalty, previous session ended without EXIT", False
        elapsed = int(time.time()) - previous.get("last_exit", 0)
        recent = bool(previous.get("last_exit")) and elapsed <= self.hop_window
        if travel:
            return (t_hopping if recent else t_login,
                    "travel from %s" % self.world_name(previous.get("world")), False)
        if (previous.get("instance") is not None
                and previous["instance"] != instance and recent):
            return t_hopping, "hop from instance %s %ds ago" % (previous["instance"], elapsed), True
        return t_login, "normal login", False

    # -- run/get/ ----------------------------------------------------------

    def run_get(self, body):
        r = Reader(body)
        pid = r.i32()
        self.session(r.i32())
        if not self.store.has_id(pid):
            raise Rejected(403, "character id %d was not issued by this hive" % pid)
        blob = self.store.load(pid)
        # The status word sent here is the one the server keeps, so the spawn
        # bits decided at ping time are repeated.
        spawn = self.pending_spawn.pop(pid, 0)
        w = Writer()
        w.u16(2)
        if blob is None or len(blob) <= Store.HEADER_LEN:
            w.i32(STATUS_NEW)
            log("run/get/       id=%d -> new character", pid)
        else:
            w.i32(STATUS_EXISTING | spawn)
            w.raw(blob[Store.HEADER_LEN:])         # the server does not expect the header back
            log("run/get/       id=%d -> %d bytes", pid, len(blob))
        return w.bytes()

    # -- run/process/ ------------------------------------------------------

    def run_process(self, body):
        try:
            payload = hive_decompress(body)
        except Exception as exc:
            log("run/process/   unreadable body: %s", exc)
            return
        r = Reader(payload)
        self.session(r.i32())
        while r.remaining() > 0:
            try:
                op = r.pack_int()
                if op == OP_SAVE:
                    size = r.i32()
                    blob = r.raw(size)
                    if size < Store.HEADER_LEN:
                        log("run/process/   truncated SAVE (%d bytes) ignored", size)
                        continue
                    player_id = struct.unpack_from("<i", blob, Store.OFF_PLAYER_ID)[0]
                    char_id = struct.unpack_from("<i", blob, Store.OFF_CHAR_ID)[0]
                    if not self.store.has_id(char_id):
                        log("run/process/   SAVE for unknown char=%d ignored", char_id)
                        continue
                    self.store.save(char_id, blob)
                    self.store.mark_saved(char_id)
                    log("run/process/   SAVE char=%d player=%d (%d bytes)",
                        char_id, player_id, size)
                elif op == OP_KILL:
                    player_id, char_id = r.i32(), r.i32()
                    if not self.store.has_id(char_id):
                        log("run/process/   KILL for unknown char=%d ignored", char_id)
                        continue
                    self.store.kill(char_id)
                    log("run/process/   KILL char=%d player=%d", char_id, player_id)
                elif op == OP_EXIT:
                    char_id = r.i32()
                    self.store.close_session(char_id)
                    log("run/process/   EXIT char=%d", char_id)
                elif op == OP_EXITGAME:
                    log("run/process/   EXITGAME (server shutting down)")
                else:
                    log("run/process/   unknown opcode %d, rest of stream dropped", op)
                    return
            except EOFError:
                return


def ago(ts):
    """Elapsed time as a short string."""
    s = max(0, int(time.time()) - int(ts or 0))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh%02dm" % (s // 3600, s % 3600 // 60)
    return "%dd%02dh" % (s // 86400, s % 86400 // 3600)


def table(headers, rows):
    esc = lambda v: html.escape(str(v))
    return ("<table><tr>%s</tr>%s</table>"
            % ("".join("<th>%s</th>" % esc(h) for h in headers),
               "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % esc(v) for v in row)
                       for row in rows) or "<tr><td colspan=%d>none</td></tr>" % len(headers)))


def status_page(hive):
    """HTML status page: missions, servers, players, recent events."""
    players = hive.store.players()
    online = [p for p in players if not p.get("exited", True)]
    by_instance = {}
    for p in online:
        by_instance[p.get("instance")] = by_instance.get(p.get("instance"), 0) + 1
    day = time.time() - 86400
    parts = [
        "<h1>DayZ private hive</h1>",
        "<p>Up for %s. %s. Token check %s, shards %s, allowed addresses %s.</p>" % (
            ago(hive.started),
            "One character per player and per map" if hive.store.world_scope
            else "One character per player for all maps",
            "on" if hive.require_token else "off",
            ", ".join(sorted(hive.shards)) or "any",
            ", ".join(str(n) for n in Handler.allowed) or "any"),
        "<h2>Missions</h2>",
        table(["Setup", "Types", "Events", "Globals", "Messages"],
              [(s, c["types"], c["events"], c["globals"], c["messages"])
               for s, _, c in sorted(hive.missions.values())]),
        "<h2>Servers</h2>",
        table(["Instance", "Name", "Map", "Shard", "Port", "Registered", "Players online"],
              [(i.get("instance_id"), i.get("hostname", ""),
                hive.world_name("%08x" % (i.get("world_hash", 0) & 0xFFFFFFFF)),
                i.get("shard_id", ""), i.get("port", ""), ago(i.get("since")) + " ago",
                by_instance.get(i.get("instance_id"), 0))
               for _, i in sorted(hive.sessions.items(),
                                  key=lambda kv: kv[1].get("instance_id", 0))]),
        "<h2>Players</h2>",
        "<p>%d character(s), %d alive, %d online now, %d seen in the last 24 h, "
        "%d death(s) in total.</p>" % (
            len(players), sum(1 for p in players if hive.store.alive(p["id"])),
            len(online), sum(1 for p in players if p.get("last_seen", 0) >= day),
            sum(p.get("deaths", 0) for p in players)),
        table(["UID", "Character", "Map", "Instance", "Alive", "Deaths", "Logged in"],
              [(p["uid"], p["id"], hive.world_name(p.get("world")), p.get("instance"),
                "yes" if hive.store.alive(p["id"]) else "no", p.get("deaths", 0),
                ago(p.get("last_seen")) + " ago")
               for p in sorted(online, key=lambda p: -p.get("last_seen", 0))]),
        "<h2>Recent events</h2>",
        "<pre>%s</pre>" % html.escape("\n".join(reversed(RECENT))),
    ]
    return ("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=30>"
            "<title>DayZ private hive</title>"
            "<style>body{font-family:sans-serif;margin:2em}table{border-collapse:collapse}"
            "td,th{border:1px solid #ccc;padding:.3em .8em;text-align:left}"
            "pre{background:#f4f4f4;padding:1em;overflow-x:auto}</style>"
            + "".join(parts))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hive = None
    prefix = "/"                # "/<secret path>/" when one is configured
    allowed = ()                # ip_network objects; empty = any client
    status_password = ""        # empty = status page disabled

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload=b""):
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _route(self):
        """Path below the secret prefix. Raises Rejected for a bad client or path."""
        if Handler.allowed:
            ip = ipaddress.ip_address(self.client_address[0])
            if not any(ip in net for net in Handler.allowed):
                raise Rejected(403, "address not allowed")
        path = urlparse(self.path).path
        if not path.startswith(Handler.prefix):
            raise Rejected(404, "unknown path")
        return path[len(Handler.prefix):].strip("/")

    def _refuse(self, exc):
        log("%s %s from %s refused (%d): %s", self.command, self.path,
            self.client_address[0], exc.code, exc)
        self.close_connection = True       # the body may not have been read
        self._send(exc.code)

    def do_POST(self):
        parsed = urlparse(self.path)
        h = Handler.hive
        try:
            route = self._route()
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise Rejected(413, "body of %d bytes" % length)
            body = self.rfile.read(length) if length else b""
            if route == "init/process":
                self._send(200, h.init_process(body))
            elif route == "init/data":
                out = h.init_data(body)
                self._send(404 if out is None else 200, out or b"")
            elif route == "run/ping":
                uid = (parse_qs(parsed.query).get("uid") or ["unknown"])[0]
                self._send(200, h.run_ping(body, uid))
            elif route == "run/get":
                self._send(200, h.run_get(body))
            elif route == "run/process":
                h.run_process(body)
                self._send(200)
            else:
                raise Rejected(404, "unknown route")
        except Rejected as exc:
            self._refuse(exc)
        except Exception as exc:
            # Always answer: a dropped connection aborts the server startup.
            import traceback
            traceback.print_exc()
            log("error on %s: %s", self.path, exc)
            self._send(500)

    def _authorized(self):
        """Basic auth against STATUS_USER and the configured password."""
        expected = "%s:%s" % (STATUS_USER, Handler.status_password)
        given = self.headers.get("Authorization", "")
        if given.startswith("Basic "):
            try:
                given = base64.b64decode(given[6:]).decode("utf-8", "replace")
            except ValueError:
                given = ""
        return hmac.compare_digest(given.encode(), expected.encode())

    def do_GET(self):
        """Status page, only with a password configured and valid basic auth."""
        try:
            self._route()
            if not Handler.status_password:
                raise Rejected(404, "status page disabled")
            if not self._authorized():
                self.close_connection = True
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="DayZ private hive"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        except Rejected as exc:
            self._refuse(exc)
            return
        body = status_page(Handler.hive).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client (or proxy) dropping the connection is not an error worth a traceback.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def setup_name(path):
    """Setup name of a mission folder: the part after the last dot."""
    return os.path.basename(os.path.normpath(path)).rsplit(".", 1)[-1]


def main():
    ap = argparse.ArgumentParser(
        description="Private hive for DayZ Server 1.29. Defaults come from config.py.")
    ap.add_argument("--mission", action="append", metavar="PATH",
                    help="mission folder to serve (repeatable)")
    ap.add_argument("--store", default=config.STORE, metavar="DIR",
                    help="where characters and sessions are saved")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--character-scope", choices=("global", "world"),
                    default=config.CHARACTER_SCOPE,
                    help="global: one survivor for all maps; world: one per map")
    ap.add_argument("--login-times", action=argparse.BooleanOptionalAction,
                    default=config.LOGIN_TIMES,
                    help="login, penalty and hopping countdowns")
    ap.add_argument("--hop-window", type=int, default=config.HOP_WINDOW, metavar="SECONDS",
                    help="a server change within this window counts as a hop")
    ap.add_argument("--hop-relocate", action=argparse.BooleanOptionalAction,
                    default=config.HOP_RELOCATE,
                    help="move a hopping player to a <hop> spawn point")
    ap.add_argument("--secret-path", default=config.SECRET_PATH, metavar="PATH",
                    help="answer only under http://HOST:PORT/PATH/")
    ap.add_argument("--require-token", action=argparse.BooleanOptionalAction,
                    default=config.REQUIRE_TOKEN,
                    help="reject requests with a token this hive did not issue")
    ap.add_argument("--shard", action="append", metavar="ID",
                    help="accept only this shardId (repeatable)")
    ap.add_argument("--allowed-ip", action="append", metavar="ADDR",
                    help="accept only this address or network (repeatable)")
    ap.add_argument("--status-password", default=config.STATUS_PASSWORD, metavar="PASSWORD",
                    help="enable the status page for user %s" % STATUS_USER)
    args = ap.parse_args()

    paths = args.mission or config.MISSIONS or sorted(glob.glob("mpmissions/dayzOnline.*"))
    paths = [p for p in paths if os.path.isdir(p)]
    if not paths:
        sys.exit("[hive] no mission folder found. Run from the DayZ server folder with a "
                 "mpmissions/dayzOnline.<map> mission, or set MISSIONS in config.py, "
                 "or pass --mission PATH.")

    world_scope = args.character_scope == "world"
    shards = args.shard or config.SHARDS
    allowed = args.allowed_ip or config.ALLOWED_IPS
    secret = (args.secret_path or "").strip("/")
    hive = Hive(Store(args.store, world_scope=world_scope),
                login_times=args.login_times, hop_window=args.hop_window,
                hop_relocate=args.hop_relocate, require_token=args.require_token,
                shards=shards)
    Handler.hive = hive
    Handler.prefix = "/%s/" % secret if secret else "/"
    Handler.allowed = tuple(ipaddress.ip_network(a, strict=False) for a in allowed)
    Handler.status_password = args.status_password or ""

    url = "http://%s:%d%s" % ("<hive ip>" if args.host == "0.0.0.0" else args.host,
                              args.port, Handler.prefix)
    for path in paths:
        setup = setup_name(path)
        if hash37(setup) in hive.missions:
            sys.exit("[hive] two missions share the setup name %r" % setup)
        counts = hive.add_mission(setup, Mission(path))
        log("mission %s -> setup %r", path, setup)
        log("  %d categories, %d events, %d globals, %d types, %d messages",
            counts["categories"], counts["events"], counts["globals"],
            counts["types"], counts["messages"])
        if counts["types"] == 0:
            log("  WARNING: no types found, the server will spawn no loot (missing db/ folder?)")
        log('  init.c:  ce.InitOnline( "%s", "%s" );', setup, url)

    log("store: %s", os.path.abspath(args.store))
    log("characters: %s", "one per player and per map" if world_scope
        else "one per player, shared by all maps")
    log("login times: %s, hop window %ds, hop relocate %s",
        "on" if args.login_times else "off", args.hop_window,
        "on" if args.hop_relocate else "off")
    log("security: secret path %s, token check %s, shards %s, allowed addresses %s",
        "on" if secret else "off", "on" if args.require_token else "off",
        ", ".join(shards) if shards else "any", ", ".join(allowed) if allowed else "any")
    log("status page: %s", "http://%s:%d%s (user %s)" % (args.host, args.port, Handler.prefix,
                                                         STATUS_USER)
        if Handler.status_password else "disabled (no STATUS_PASSWORD)")
    log("listening on http://%s:%d%s", args.host, args.port, Handler.prefix)

    srv = Server((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
