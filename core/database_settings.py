"""Single side-effect-free PostgreSQL settings source."""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> "DatabaseSettings":
        return cls(
            host=os.getenv("PG_HOST", "127.0.0.1").strip(),
            port=int(os.getenv("PG_PORT", "55432") or "55432"),
            dbname=os.getenv("PG_DATABASE", "").strip(),
            user=os.getenv("PG_USER", "").strip(),
            password=os.getenv("PG_PASSWORD", ""),
        )

    def connect_kwargs(self) -> dict:
        if not self.dbname or not self.user:
            raise RuntimeError("PostgreSQL configuration is incomplete")
        return {
            "host": self.host, "port": self.port, "dbname": self.dbname,
            "user": self.user, "password": self.password,
        }

    def safe_dict(self) -> dict:
        return {"host": self.host, "port": self.port,
                "dbname": self.dbname, "user": self.user}
