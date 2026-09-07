"""Reads the db/*.xml files of a mission and encodes the five init/data blocks.

In ONLINE mode the DayZ server never reads db/ itself: economy, events,
globals, types and messages all come from the hive.
"""

import os
import xml.etree.ElementTree as ET

from wire import Writer, hash37_signed, pack_int_bytes

# Server-side buffer sizes. Longer names overflow the server parser, so they are dropped.
MAX_CATEGORY_NAME = 39
MAX_EVENT_NAME = 31
MAX_EVENT_SECONDARY = 39
MAX_GLOBAL_NAME = 31
MAX_MESSAGE_TEXT = 1151

# db/economy.xml category attributes
CE_INIT, CE_LOAD, CE_SAVE, CE_RESPAWN = 0x01, 0x02, 0x04, 0x10

# db/types.xml <flags>
TYPE_FLAGS = [
    ("count_in_map", 0x01),
    ("count_in_cargo", 0x02),
    ("count_in_player", 0x04),
    ("count_in_hoarder", 0x08),
    ("deloot", 0x40),
    ("crafted", 0x80),
]
TYPE_CRAFTED = 0x80

# db/events.xml <flags>, plus <position> and <limit> folded into the same integer
EVENT_FLAGS = [("init_random", 0x01), ("deletable", 0x02), ("remove_damaged", 0x04)]
EVENT_POSITION = {"fixed": 0x100, "player": 0x200, "uniform": 0x400}
EVENT_LIMIT = {"parent": 0x10000, "child": 0x20000,
               "mixed": 0x40000, "custom": 0x80000}

# db/messages.xml
MSG_ONCONNECT, MSG_REPEAT, MSG_COUNTDOWN, MSG_SHUTDOWN, MSG_TIMED = (
    0x01, 0x02, 0x04, 0x08, 0x10)


def _text(node, tag, default=0):
    el = node.find(tag)
    if el is None or el.text is None or not el.text.strip():
        return default
    return int(float(el.text.strip()))


def _child_text(node, tag, default=""):
    el = node.find(tag)
    if el is None or el.text is None:
        return default
    return el.text.strip().lower()


def _attr_flags(node, tag, table):
    el = node.find(tag)
    value = 0
    if el is not None:
        for name, bit in table:
            if el.get(name, "0").strip() not in ("0", "", "false"):
                value |= bit
    return value


class Mission:
    """Central economy data of one mission folder."""

    def __init__(self, path):
        self.path = path
        self.categories = []
        self.events = []
        self.globals = []
        self.types = []
        self.messages = []
        self.limits = {"category": {}, "tag": {}, "usage": {}, "value": {}}
        self._load()

    def _root(self, *parts):
        f = os.path.join(self.path, *parts)
        if not os.path.isfile(f):
            return None
        return ET.parse(f).getroot()

    def _load(self):
        self._load_limits()
        self._load_economy()
        self._load_events()
        self._load_globals()
        self._load_types()
        self._load_messages()

    def _load_limits(self):
        """cfglimitsdefinition.xml: category -> index, tag/usage/value -> bit."""
        root = self._root("cfglimitsdefinition.xml")
        if root is None:
            return
        spec = [("categories", "category", "category", False),
                ("tags", "tag", "tag", True),
                ("usageflags", "usage", "usage", True),
                ("valueflags", "value", "value", True)]
        for container, child, key, as_bit in spec:
            node = root.find(container)
            if node is None:
                continue
            for i, el in enumerate(node.findall(child)):
                name = el.get("name")
                if name:
                    self.limits[key][name.lower()] = (1 << i) if as_bit else i

        # cfglimitsdefinitionuser.xml: aliases combining several bits
        user = self._root("cfglimitsdefinitionuser.xml")
        if user is None:
            return
        for container, child, key in (("usageflags", "usage", "usage"),
                                      ("valueflags", "value", "value")):
            node = user.find(container)
            if node is None:
                continue
            for el in node.findall("user"):
                alias = el.get("name")
                mask = 0
                for sub in el.findall(child):
                    mask |= self.limits[key].get((sub.get("name") or "").lower(), 0)
                if alias:
                    self.limits[key][alias.lower()] = mask

    def _load_economy(self):
        root = self._root("db", "economy.xml")
        if root is None:
            return
        for el in root:
            if not isinstance(el.tag, str):
                continue
            flags = 0
            if el.get("init", "0") == "1":
                flags |= CE_INIT
            if el.get("load", "0") == "1":
                flags |= CE_LOAD
            if el.get("save", "0") == "1":
                flags |= CE_SAVE
            if el.get("respawn", "0") == "1":
                flags |= CE_RESPAWN
            self.categories.append((el.tag, flags))

    def _load_events(self):
        root = self._root("db", "events.xml")
        if root is None:
            return
        for el in root.findall("event"):
            if _text(el, "active", 1) != 1:
                continue
            sec = el.find("secondary")
            children = []
            group = el.find("children")
            if group is not None:
                for c in group.findall("child"):
                    children.append({
                        "type": c.get("type", ""),
                        "min": int(c.get("min", 0)),
                        "max": int(c.get("max", 0)),
                        "lootmin": int(c.get("lootmin", 0)),
                        "lootmax": int(c.get("lootmax", 0)),
                    })
            self.events.append({
                "name": el.get("name", ""),
                "nominal": _text(el, "nominal"),
                "min": _text(el, "min"),
                "max": _text(el, "max"),
                "lifetime": _text(el, "lifetime"),
                "restock": _text(el, "restock"),
                "saferadius": _text(el, "saferadius"),
                "distanceradius": _text(el, "distanceradius"),
                "cleanupradius": _text(el, "cleanupradius"),
                "flags": (_attr_flags(el, "flags", EVENT_FLAGS)
                          | EVENT_POSITION.get(_child_text(el, "position"),
                                               EVENT_POSITION["fixed"])
                          | EVENT_LIMIT.get(_child_text(el, "limit"),
                                            EVENT_LIMIT["mixed"])),
                "secondary": (sec.text or "").strip() if sec is not None else "",
                "children": children,
            })

    def _load_globals(self):
        root = self._root("db", "globals.xml")
        if root is None:
            return
        for el in root.findall("var"):
            name = el.get("name", "")
            raw = el.get("value", "0")
            if el.get("type") == "2":
                self.globals.append((name, "str", raw))
            elif el.get("type") == "1":
                self.globals.append((name, "float", float(raw)))
            else:
                self.globals.append((name, "int", int(float(raw))))

    def _load_types(self):
        root = self._root("db", "types.xml")
        if root is None:
            return
        cat, tag, usage, value = (self.limits["category"], self.limits["tag"],
                                  self.limits["usage"], self.limits["value"])
        for el in root.findall("type"):
            name = el.get("name", "")
            cat_el = el.find("category")
            cat_idx = -1
            if cat_el is not None:
                cat_idx = cat.get((cat_el.get("name") or "").lower(), -1)
            tag_mask = 0
            for t in el.findall("tag"):
                tag_mask |= tag.get((t.get("name") or "").lower(), 0)
            usage_mask = 0
            for u in el.findall("usage"):
                usage_mask |= usage.get((u.get("name") or "").lower(), 0)
            value_mask = 0
            for v in el.findall("value"):
                value_mask |= value.get((v.get("name") or "").lower(), 0)
            self.types.append({
                "name": name,
                "hash": hash37_signed(name),
                "flags": _attr_flags(el, "flags", TYPE_FLAGS),
                "lifetime": _text(el, "lifetime"),
                "nominal": _text(el, "nominal"),
                "restock": _text(el, "restock"),
                "min": _text(el, "min"),
                "quantmin": _text(el, "quantmin", -1),
                "quantmax": _text(el, "quantmax", -1),
                "cost": _text(el, "cost", 100),
                "category": cat_idx,
                "tag": tag_mask,
                "usage": usage_mask,
                "value": value_mask,
            })

    def _load_messages(self):
        root = self._root("db", "messages.xml")
        if root is None:
            return
        for el in root.findall("message"):
            text_el = el.find("text")
            if text_el is None or not (text_el.text or "").strip():
                continue
            delay = _text(el, "delay")
            repeat = _text(el, "repeat")
            deadline = _text(el, "deadline")
            flags = 0
            if _text(el, "onconnect") == 1:
                flags |= MSG_ONCONNECT
            if repeat > 0:
                flags |= MSG_REPEAT
            if deadline > 0:
                flags |= MSG_COUNTDOWN
            if _text(el, "shutdown") == 1:
                flags |= MSG_SHUTDOWN
            # A message with none of these bits is never shown by the server.
            if not flags & (MSG_ONCONNECT | MSG_REPEAT | MSG_COUNTDOWN | MSG_TIMED):
                flags |= MSG_TIMED
            self.messages.append({
                "delay": delay, "repeat": repeat, "countdown": deadline,
                "flags": flags, "text": (text_el.text or "").strip(),
            })

    # The five blocks are read by the server with a single cursor and no
    # length markers: any misencoding silently corrupts everything after it.

    def block1_categories(self, w):
        rows = [(n, f) for n, f in self.categories
                if len(n.encode()) <= MAX_CATEGORY_NAME]
        w.i32(len(rows))
        for name, flags in rows:
            w.small_string(name).i32(flags)
        return len(rows)

    def block2_events(self, w):
        rows = [e for e in self.events
                if len(e["name"].encode()) <= MAX_EVENT_NAME
                and len(e["secondary"].encode()) <= MAX_EVENT_SECONDARY]
        w.i32(len(rows))
        for e in rows:
            w.small_string(e["name"])
            for key in ("nominal", "min", "max", "lifetime", "restock",
                        "saferadius", "distanceradius", "cleanupradius", "flags"):
                w.pack_int(e[key])
            w.small_string(e["secondary"])
            w.i32(len(e["children"]))
            for c in e["children"]:
                w.pack_int(hash37_signed(c["type"]))
                w.pack_int(c["min"]).pack_int(c["max"])
                w.pack_int(c["lootmin"]).pack_int(c["lootmax"])
        return len(rows)

    def block3_globals(self, w):
        rows, seen = [], set()
        for name, kind, value in self.globals:
            key = name.lower()
            if key in seen or len(name.encode()) > MAX_GLOBAL_NAME:
                continue
            seen.add(key)
            rows.append((name, kind, value))
        w.i32(len(rows))
        for name, kind, value in rows:
            w.small_string(name)
            if kind == "float":
                w.var_float(value)
            elif kind == "str":
                w.var_string(value)
            else:
                w.var_int(value)
        return len(rows)

    def block4_types(self, w):
        w.i32(len(self.types))
        for t in self.types:
            crafted = bool(t["flags"] & TYPE_CRAFTED)
            tail = []
            if not crafted:
                tail = [t["nominal"], t["restock"], t["min"], t["quantmin"],
                        t["quantmax"], t["cost"], t["category"], t["tag"],
                        t["usage"], t["value"]]
            # tail length must be exact, and 0 for crafted types
            tail_len = sum(pack_int_bytes(v) for v in tail)
            w.pack_int(t["hash"]).pack_int(t["flags"]).pack_int(t["lifetime"])
            w.u8(tail_len if tail_len <= 255 else 0)
            for v in tail:
                w.pack_int(v)
        return len(self.types)

    def block5_messages(self, w):
        rows = [m for m in self.messages
                if len(m["text"].encode()) <= MAX_MESSAGE_TEXT]
        w.i32(len(rows))
        for m in rows:
            w.pack_int(m["delay"]).pack_int(m["repeat"])
            w.pack_int(m["countdown"]).pack_int(m["flags"])
            w.string(m["text"])
        return len(rows)

    def init_data_payload(self):
        """Uncompressed body of the init/data response, plus row counts per block."""
        w = Writer()
        counts = {
            "categories": self.block1_categories(w),
            "events": self.block2_events(w),
            "globals": self.block3_globals(w),
            "types": self.block4_types(w),
            "messages": self.block5_messages(w),
        }
        return w.bytes(), counts

    def has_shutdown_message(self):
        """True if a shutdown message exists that the server can actually display."""
        return any(m["flags"] & MSG_SHUTDOWN
                   and m["flags"] & (MSG_COUNTDOWN | MSG_TIMED)
                   for m in self.messages)
