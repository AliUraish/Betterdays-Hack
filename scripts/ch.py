"""Tiny ClickHouse helper shared by scripts: env-driven client (local or ClickHouse Cloud) + SQL-file runner."""
import os, re, sys
from pathlib import Path
from dotenv import load_dotenv
import clickhouse_connect

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

def client():
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost").replace("https://", "").replace("http://", "").strip("/"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        secure=os.getenv("CLICKHOUSE_SECURE", "0") == "1",
            autogenerate_session_id=False,   # stateless queries -> safe across FastAPI worker threads
        connect_timeout=20, send_receive_timeout=300,
    )

def statements(path):
    sql = Path(path).read_text()
    sql = re.sub(r"--.*$", "", sql, flags=re.M)                # drop -- comments (line and inline)
    return [s.strip() for s in sql.split(";") if s.strip()]

def run_file(ch, path):
    for s in statements(path):
        head = s.split("\n")[0][:80]
        ch.command(s)
        print("OK ", head)

if __name__ == "__main__":
    ch = client()
    for p in sys.argv[1:]:
        run_file(ch, p)
