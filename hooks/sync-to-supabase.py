#!/usr/bin/env python3
"""
Sync local SQLite data to Supabase for the lab dashboard.

Syncs:
- agent_transcripts: Subagent execution transcripts
- encoding_events: File writes, stub creation, test runs, beads creation

Run manually: python3 sync-to-supabase.py
Or via: autorac sync-transcripts
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    from supabase import create_client, Client
except ImportError:
    print("Installing supabase-py...")
    os.system("pip install supabase")
    from supabase import create_client, Client

# Configuration
LOCAL_DB = Path.home() / "RulesFoundation" / "autorac" / "transcripts.db"
SUPABASE_URL = "https://nsupqhfchdtqclomlrgs.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")


# ============================================
# TRANSCRIPTS
# ============================================

def get_unsynced_transcripts(conn: sqlite3.Connection) -> list[dict]:
    """Get transcripts that haven't been uploaded yet."""
    cursor = conn.execute("""
        SELECT id, session_id, agent_id, tool_use_id, subagent_type,
               prompt, description, response_summary, transcript,
               orchestrator_thinking, message_count, created_at
        FROM agent_transcripts
        WHERE uploaded_at IS NULL
        ORDER BY id
    """)

    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def mark_as_uploaded(conn: sqlite3.Connection, ids: list[int]):
    """Mark transcripts as uploaded."""
    now = datetime.utcnow().isoformat()
    conn.executemany(
        "UPDATE agent_transcripts SET uploaded_at = ? WHERE id = ?",
        [(now, id) for id in ids]
    )
    conn.commit()


def sync_transcripts_to_supabase(conn: sqlite3.Connection, supabase: Client):
    """Sync local transcripts to Supabase."""
    transcripts = get_unsynced_transcripts(conn)

    if not transcripts:
        print("No new transcripts to sync")
        return

    print(f"Found {len(transcripts)} transcripts to sync")

    # Transform for Supabase schema
    records = []
    for t in transcripts:
        # Parse transcript JSON string to dict for proper JSONB storage
        transcript_data = json.loads(t["transcript"]) if isinstance(t["transcript"], str) else t["transcript"]

        records.append({
            "session_id": t["session_id"],
            "agent_id": t["agent_id"],
            "tool_use_id": t["tool_use_id"],
            "subagent_type": t["subagent_type"],
            "prompt": t["prompt"],
            "description": t["description"],
            "response_summary": t["response_summary"],
            "transcript": transcript_data,  # Pass as dict, not JSON string
            "orchestrator_thinking": t.get("orchestrator_thinking", ""),
            "message_count": t["message_count"],
            "created_at": t["created_at"],
        })

    # Upload to Supabase
    try:
        result = supabase.table("agent_transcripts").upsert(
            records,
            on_conflict="tool_use_id"
        ).execute()

        print(f"Uploaded {len(records)} transcripts to Supabase")

        # Mark as uploaded locally
        mark_as_uploaded(conn, [t["id"] for t in transcripts])
        print("Marked as uploaded in local DB")

    except Exception as e:
        print(f"Error uploading to Supabase: {e}")
        print("You may need to create the agent_transcripts table in Supabase first.")
        print("""
CREATE TABLE agent_transcripts (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    tool_use_id TEXT UNIQUE NOT NULL,
    subagent_type TEXT NOT NULL,
    prompt TEXT,
    description TEXT,
    response_summary TEXT,
    transcript JSONB,
    message_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    uploaded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_agent_transcripts_session ON agent_transcripts(session_id);
CREATE INDEX idx_agent_transcripts_agent ON agent_transcripts(agent_id);
CREATE INDEX idx_agent_transcripts_type ON agent_transcripts(subagent_type);

ALTER TABLE agent_transcripts ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow service access to agent_transcripts"
ON agent_transcripts
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);
        """)


# ============================================
# ENCODING EVENTS
# ============================================

def get_unsynced_events(conn: sqlite3.Connection) -> list[dict]:
    """Get encoding events that haven't been uploaded yet."""
    try:
        cursor = conn.execute("""
            SELECT id, session_id, event_type, file_path, metadata, created_at
            FROM encoding_events
            WHERE uploaded_at IS NULL
            ORDER BY id
        """)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        # Table doesn't exist yet
        return []


def mark_events_uploaded(conn: sqlite3.Connection, ids: list[int]):
    """Mark events as uploaded."""
    now = datetime.utcnow().isoformat()
    conn.executemany(
        "UPDATE encoding_events SET uploaded_at = ? WHERE id = ?",
        [(now, id) for id in ids]
    )
    conn.commit()


def sync_events_to_supabase(conn: sqlite3.Connection, supabase: Client):
    """Sync encoding events to Supabase."""
    events = get_unsynced_events(conn)

    if not events:
        print("No new encoding events to sync")
        return

    print(f"Found {len(events)} encoding events to sync")

    # Transform for Supabase schema
    records = []
    for e in events:
        metadata = json.loads(e["metadata"]) if e["metadata"] else None
        records.append({
            "session_id": e["session_id"],
            "event_type": e["event_type"],
            "file_path": e["file_path"],
            "metadata": metadata,
            "created_at": e["created_at"],
        })

    try:
        supabase.table("encoding_events").insert(records).execute()
        print(f"Uploaded {len(records)} encoding events to Supabase")
        mark_events_uploaded(conn, [e["id"] for e in events])
        print("Marked events as uploaded in local DB")
    except Exception as e:
        print(f"Error uploading encoding events: {e}")
        print("You may need to create the encoding_events table in Supabase first.")
        print("""
CREATE TABLE encoding_events (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    file_path TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_encoding_events_session ON encoding_events(session_id);
CREATE INDEX idx_encoding_events_type ON encoding_events(event_type);
CREATE INDEX idx_encoding_events_file ON encoding_events(file_path);
        """)


# ============================================
# MAIN
# ============================================

def sync_all():
    """Sync all local data to Supabase."""
    if not SUPABASE_KEY:
        print("Error: No Supabase key found. Set SUPABASE_SERVICE_KEY")
        sys.exit(1)

    if not LOCAL_DB.exists():
        print(f"No local database at {LOCAL_DB}")
        sys.exit(0)

    conn = sqlite3.connect(str(LOCAL_DB))
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Sync transcripts
    sync_transcripts_to_supabase(conn, supabase)

    # Sync encoding events
    sync_events_to_supabase(conn, supabase)

    conn.close()
    print("\nSync complete!")


if __name__ == "__main__":
    sync_all()
