from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS speakers (
    device_id TEXT PRIMARY KEY,
    ip TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    firmware TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    cloud_url TEXT NOT NULL DEFAULT '',
    migrated INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS presets (
    device_id TEXT NOT NULL,
    slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 6),
    source TEXT NOT NULL DEFAULT 'EMPTY',
    name TEXT NOT NULL DEFAULT '',
    station_id TEXT NOT NULL DEFAULT '',
    stream_url TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    raw_content_item TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (device_id, slot),
    FOREIGN KEY (device_id) REFERENCES speakers(device_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS siriusxm_channels (
    station_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    entity_url TEXT NOT NULL DEFAULT '',
    stream_url TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scmudc_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def upsert_speaker(self, speaker: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO speakers(device_id, ip, name, model, firmware, account_id, cloud_url, migrated)
            VALUES(:device_id, :ip, :name, :model, :firmware, :account_id, :cloud_url, :migrated)
            ON CONFLICT(device_id) DO UPDATE SET
                ip=excluded.ip,
                name=excluded.name,
                model=excluded.model,
                firmware=excluded.firmware,
                account_id=excluded.account_id,
                cloud_url=COALESCE(NULLIF(excluded.cloud_url, ''), speakers.cloud_url),
                migrated=MAX(speakers.migrated, excluded.migrated),
                updated_at=CURRENT_TIMESTAMP
            """,
            {
                "device_id": speaker.get("device_id", ""),
                "ip": speaker.get("ip", ""),
                "name": speaker.get("name", ""),
                "model": speaker.get("model", ""),
                "firmware": speaker.get("firmware", ""),
                "account_id": speaker.get("account_id", ""),
                "cloud_url": speaker.get("cloud_url", ""),
                "migrated": 1 if speaker.get("migrated") else 0,
            },
        )
        self.conn.commit()

    def set_migrated(self, device_id: str, base_url: str) -> None:
        self.conn.execute(
            "UPDATE speakers SET migrated=1, cloud_url=?, updated_at=CURRENT_TIMESTAMP WHERE device_id=?",
            (base_url, device_id),
        )
        self.conn.commit()

    def list_speakers(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM speakers ORDER BY name, ip").fetchall()
        return [dict(row) for row in rows]

    def get_speaker(self, device_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM speakers WHERE device_id=?", (device_id,)).fetchone()
        return dict(row) if row else None

    def speakers_for_account(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM speakers WHERE account_id=? OR ?='' ORDER BY name, ip",
            (account_id, account_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def replace_presets(self, device_id: str, presets: list[dict[str, Any]]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM presets WHERE device_id=?", (device_id,))
            for preset in presets:
                self._set_preset_locked(device_id, preset)

    def set_preset(self, device_id: str, preset: dict[str, Any]) -> dict[str, Any]:
        with self.conn:
            self._set_preset_locked(device_id, preset)
        return self.get_preset(device_id, int(preset["slot"]))

    def clear_preset(self, device_id: str, slot: int) -> None:
        self.conn.execute("DELETE FROM presets WHERE device_id=? AND slot=?", (device_id, slot))
        self.conn.commit()

    def copy_preset(self, device_id: str, source_slot: int, target_slot: int) -> dict[str, Any]:
        preset = self.get_preset(device_id, source_slot)
        if preset["source"] == "EMPTY":
            raise ValueError("source slot is empty")
        preset["slot"] = target_slot
        preset["device_id"] = device_id
        return self.set_preset(device_id, preset)

    def get_preset(self, device_id: str, slot: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM presets WHERE device_id=? AND slot=?",
            (device_id, slot),
        ).fetchone()
        if row:
            return _normalize_preset_row(dict(row))
        return {
            "device_id": device_id,
            "slot": slot,
            "source": "EMPTY",
            "name": "",
            "station_id": "",
            "stream_url": "",
            "image_url": "",
            "raw_content_item": "",
        }

    def preset_slots_for_speaker(self, device_id: str) -> list[dict[str, Any]]:
        found = {int(p["slot"]): p for p in self.presets_for_speaker(device_id)}
        return [found.get(slot) or self.get_preset(device_id, slot) for slot in range(1, 7)]

    def presets_for_speaker(self, device_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM presets WHERE device_id=? ORDER BY slot",
            (device_id,),
        ).fetchall()
        return [_normalize_preset_row(dict(row)) for row in rows]

    def find_preset_by_source_station(self, source: str, station_id: str) -> dict[str, Any] | None:
        rows = self.conn.execute(
            "SELECT * FROM presets WHERE source=? OR raw_content_item LIKE ? ORDER BY slot",
            (source, f"%{source}%"),
        ).fetchall()
        normalized = [_normalize_preset_row(dict(row)) for row in rows]
        for preset in normalized:
            if preset.get("source") != source:
                continue
            stored_id = str(preset.get("station_id", ""))
            if stored_id == station_id or stored_id.split("?", 1)[0] == station_id:
                return preset
            raw_location = _xml_attr(str(preset.get("raw_content_item", "")), "location")
            raw_slug = raw_location.rstrip("/").split("/")[-1].split("?", 1)[0]
            if raw_slug == station_id:
                return preset
        return None

    def upsert_siriusxm_channel(self, station_id: str, data: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT INTO siriusxm_channels(station_id, name, entity_url, stream_url)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(station_id) DO UPDATE SET
                name=excluded.name,
                entity_url=excluded.entity_url,
                stream_url=excluded.stream_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                station_id,
                str(data.get("name", "")),
                str(data.get("entity_url", "")),
                str(data.get("stream_url", "")),
            ),
        )
        self.conn.commit()
        return self.get_siriusxm_channel(station_id)

    def get_siriusxm_channel(self, station_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM siriusxm_channels WHERE station_id=?",
            (station_id,),
        ).fetchone()
        if row:
            return dict(row)
        return {"station_id": station_id, "name": "", "entity_url": "", "stream_url": ""}

    def list_siriusxm_channels(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM siriusxm_channels ORDER BY station_id").fetchall()
        return [dict(row) for row in rows]

    def add_scmudc_event(self, device_id: str, summary: str, body: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO scmudc_events(device_id, summary, body) VALUES(?, ?, ?)",
                (device_id, summary, body),
            )
            self.conn.execute(
                """
                DELETE FROM scmudc_events
                WHERE id NOT IN (
                    SELECT id FROM scmudc_events
                    WHERE device_id=?
                    ORDER BY id DESC
                    LIMIT 50
                )
                AND device_id=?
                """,
                (device_id, device_id),
            )

    def recent_scmudc_events(self, device_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, device_id, received_at, summary, body
            FROM scmudc_events
            WHERE device_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (device_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def _set_preset_locked(self, device_id: str, preset: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO presets(device_id, slot, source, name, station_id, stream_url, image_url, raw_content_item)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, slot) DO UPDATE SET
                source=excluded.source,
                name=excluded.name,
                station_id=excluded.station_id,
                stream_url=excluded.stream_url,
                image_url=excluded.image_url,
                raw_content_item=excluded.raw_content_item
            """,
            (
                device_id,
                int(preset.get("slot", 0)),
                preset.get("source", "EMPTY"),
                preset.get("name", ""),
                preset.get("station_id", ""),
                preset.get("stream_url", ""),
                preset.get("image_url", ""),
                preset.get("raw_content_item", ""),
            ),
        )


def _normalize_preset_row(preset: dict[str, Any]) -> dict[str, Any]:
    raw = str(preset.get("raw_content_item", ""))
    if "SIRIUSXM_EVEREST" in raw:
        preset["source"] = "SIRIUSXM"
        location = _xml_attr(raw, "location")
        if location:
            preset["station_id"] = location.rstrip("/").split("/")[-1]
    return preset


def _xml_attr(text: str, name: str) -> str:
    match = re.search(rf'{re.escape(name)}=(?:"([^"]*)"|\'([^\']*)\')', text, re.I)
    if not match:
        return ""
    value = match.group(1) if match.group(1) is not None else match.group(2)
    return (
        value.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )
