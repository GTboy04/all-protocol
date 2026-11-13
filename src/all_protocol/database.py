"""SQLite storage primitives. Each operation owns its connection and transaction."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


DATABASE_NAME = "allprotocol.sqlite3"
SCHEMA_VERSION = 1
APPLICATION_ID = 0x41505254
SQLITE_HEADER = b"SQLite format 3\x00"

SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE connections (
        position INTEGER PRIMARY KEY AUTOINCREMENT, protocol TEXT NOT NULL,
        endpoint TEXT NOT NULL, fields TEXT NOT NULL, interval_ms INTEGER NOT NULL,
        UNIQUE(protocol, endpoint))""",
    """CREATE TABLE server_plans (
        protocol TEXT PRIMARY KEY, version INTEGER NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL)""",
    """CREATE TABLE server_stations (
        protocol TEXT NOT NULL REFERENCES server_plans ON DELETE CASCADE,
        position INTEGER NOT NULL, unit_id INTEGER NOT NULL, name TEXT NOT NULL,
        PRIMARY KEY(protocol, position))""",
    """CREATE TABLE server_points (
        protocol TEXT NOT NULL REFERENCES server_plans ON DELETE CASCADE,
        station INTEGER NOT NULL, position INTEGER NOT NULL, definition TEXT NOT NULL,
        PRIMARY KEY(protocol, station, position))""",
    """CREATE TABLE simulation_rules (
        protocol TEXT NOT NULL, station INTEGER NOT NULL, position INTEGER NOT NULL, rule TEXT NOT NULL,
        PRIMARY KEY(protocol, station, position),
        FOREIGN KEY(protocol, station, position) REFERENCES server_points ON DELETE CASCADE)""",
    """CREATE TABLE journal_state (
        stream TEXT PRIMARY KEY, rotated_at TEXT, incomplete INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE journal_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL REFERENCES journal_state,
        time TEXT NOT NULL, protocol TEXT NOT NULL, endpoint TEXT NOT NULL,
        payload TEXT NOT NULL, size_bytes INTEGER NOT NULL)""",
    "CREATE INDEX journal_stream_recent ON journal_entries(stream, id DESC)",
    """CREATE TABLE devices (
        id TEXT PRIMARY KEY, position INTEGER UNIQUE NOT NULL, name TEXT NOT NULL,
        protocol TEXT NOT NULL, endpoint TEXT NOT NULL, fields TEXT NOT NULL,
        interval_ms INTEGER NOT NULL, UNIQUE(protocol, endpoint))""",
    """CREATE TABLE point_tables (
        id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices ON DELETE CASCADE,
        position INTEGER UNIQUE NOT NULL, definition TEXT NOT NULL)""",
    """CREATE TABLE named_points (
        id TEXT PRIMARY KEY, table_id TEXT NOT NULL REFERENCES point_tables ON DELETE CASCADE,
        position INTEGER NOT NULL, definition TEXT NOT NULL, UNIQUE(table_id, position))""",
)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def initialize(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    app_id = connection.execute("PRAGMA application_id").fetchone()[0]
    if version == 0 and app_id == 0 and not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table'"
    ).fetchone():
        for statement in SCHEMA:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    else:
        validate(connection)


def validate(connection):
    if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
        raise ValueError("SQLite 数据库格式或版本无法识别")


@contextmanager
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = None
    try:
        connection = sqlite3.connect(path, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        initialize(connection)
        kind = get_meta(connection, "kind")
        if kind not in (None, "local"):
            raise ValueError("本地数据目录中的 SQLite 文件不是本地数据库")
        if kind is None:
            set_meta(connection, "kind", "local")
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        raise OSError(f"SQLite 数据读写失败：{error}") from error
    finally:
        if connection is not None:
            connection.close()


def get_meta(connection, key):
    row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(connection, key, value):
    connection.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, value))


def write_plan(connection, record):
    protocol = record["protocol"]
    connection.execute("DELETE FROM server_plans WHERE protocol=?", (protocol,))
    connection.execute("INSERT INTO server_plans VALUES (?, ?, ?, ?)",
                       (protocol, record["schema_version"], record["host"], record["port"]))
    groups = record["stations"] if protocol == "modbus-tcp" else [{"points": record["points"]}]
    for station, group in enumerate(groups):
        if protocol == "modbus-tcp":
            connection.execute("INSERT INTO server_stations VALUES (?, ?, ?, ?)",
                               (protocol, station, group["unit_id"], group["name"]))
        for position, point in enumerate(group["points"]):
            definition = {key: value for key, value in point.items() if key != "simulation"}
            connection.execute("INSERT INTO server_points VALUES (?, ?, ?, ?)",
                               (protocol, station, position, dumps(definition)))
            if point.get("simulation") is not None:
                connection.execute("INSERT INTO simulation_rules VALUES (?, ?, ?, ?)",
                                   (protocol, station, position, dumps(point["simulation"])))


def read_plan(connection, protocol):
    row = connection.execute("SELECT version, host, port FROM server_plans WHERE protocol=?", (protocol,)).fetchone()
    if row is None:
        return None
    record = dict(schema_version=row[0], protocol=protocol, host=row[1], port=row[2])
    groups = {}
    for station, definition, rule in connection.execute(
        """SELECT p.station, p.definition, r.rule FROM server_points p
           LEFT JOIN simulation_rules r USING(protocol, station, position)
           WHERE p.protocol=? ORDER BY p.station, p.position""", (protocol,)
    ):
        point = json.loads(definition)
        if rule is not None:
            point["simulation"] = json.loads(rule)
        groups.setdefault(station, []).append(point)
    if protocol == "modbus-tcp":
        record["stations"] = [dict(unit_id=unit, name=name, points=groups.get(position, []))
                              for position, unit, name in connection.execute(
                                  "SELECT position, unit_id, name FROM server_stations WHERE protocol=? ORDER BY position",
                                  (protocol,))]
    else:
        record["points"] = groups.get(0, [])
    return record


def encode_project_database(record):
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize(connection)
        set_meta(connection, "kind", "project")
        set_meta(connection, "name", record["name"])
        if "workspace_layout" in record:
            set_meta(connection, "workspace_layout", dumps(record["workspace_layout"]))
        for position, device in enumerate(record["devices"]):
            profile = device["profile"]
            connection.execute("INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (device["id"], position, device["name"], profile["protocol"],
                                profile["endpoint"], dumps(profile["fields"]), profile["interval_ms"]))
        for plan in record["servers"].values():
            write_plan(connection, plan)
        for position, table in enumerate(record["point_tables"]):
            definition = {key: value for key, value in table.items() if key not in ("id", "device_id", "points")}
            connection.execute("INSERT INTO point_tables VALUES (?, ?, ?, ?)",
                               (table["id"], table["device_id"], position, dumps(definition)))
            for index, point in enumerate(table["points"]):
                connection.execute("INSERT INTO named_points VALUES (?, ?, ?, ?)",
                                   (point["id"], table["id"], index,
                                    dumps({key: value for key, value in point.items() if key != "id"})))
        connection.commit()
        return connection.serialize()
    except sqlite3.Error as error:
        raise OSError(f"项目 SQLite 保存失败：{error}") from error
    finally:
        connection.close()


def decode_project_database(contents):
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(contents)
        connection.execute("PRAGMA query_only=ON")
        validate(connection)
        if get_meta(connection, "kind") != "project":
            raise ValueError("所选 SQLite 文件不是项目")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",) or connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchone():
            raise ValueError("项目数据库完整性检查失败")
        devices = [dict(id=id_, name=name, profile=dict(protocol=protocol, endpoint=endpoint,
                                                      fields=json.loads(fields), interval_ms=interval))
                   for id_, name, protocol, endpoint, fields, interval in connection.execute(
                       "SELECT id, name, protocol, endpoint, fields, interval_ms FROM devices ORDER BY position")]
        tables = []
        for id_, device_id, definition in connection.execute(
            "SELECT id, device_id, definition FROM point_tables ORDER BY position"
        ):
            points = [dict(json.loads(definition), id=point_id) for point_id, definition in connection.execute(
                "SELECT id, definition FROM named_points WHERE table_id=? ORDER BY position", (id_,))]
            tables.append(dict(json.loads(definition), id=id_, device_id=device_id, points=points))
        record = dict(format="all-protocol-project", schema_version=3, name=get_meta(connection, "name"),
                      devices=devices, servers={protocol: read_plan(connection, protocol)
                                                for protocol in ("modbus-tcp", "s7")}, point_tables=tables)
        layout = get_meta(connection, "workspace_layout")
        if layout is not None:
            record["workspace_layout"] = json.loads(layout)
        return record
    except sqlite3.Error as error:
        raise ValueError(f"项目 SQLite 数据库无效：{error}") from error
    finally:
        connection.close()
