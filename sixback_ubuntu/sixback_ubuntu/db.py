from __future__ import annotations

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
                self.conn.execute(
                    """
                    INSERT INTO presets(device_id, slot, source, name, station_id, stream_url, image_url, raw_content_item)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
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

    def presets_for_speaker(self, device_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM presets WHERE device_id=? ORDER BY slot",
            (device_id,),
        ).fetchall()
        return [dict(row) for row in rows]
