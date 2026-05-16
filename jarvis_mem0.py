"""
Jarvis — Mem0 Vector Memory
============================
Replaces the text-file memory system (episodic.md, semantic.md)
with a proper vector database. Memories are searchable by semantic
similarity — Jarvis can now accurately recall specific things from
weeks ago by searching for relevant context.

HOW IT WORKS:
  - Nightly: memory_system.py calls add_memories() with today's data
  - At brief/voice time: search_memories() finds the most relevant memories
  - Memories are stored in memory/mem0_db/ (local Chroma vector DB)

COST:
  - Claude Haiku for memory extraction: ~$0.001 per nightly update
  - Embeddings: free (local sentence-transformers model)
  - Storage: local, no cloud

CLI:
  python jarvis_mem0.py --search "Pokemon reselling"
  python jarvis_mem0.py --all
  python jarvis_mem0.py --add "I got a callback from Canva today"
  python jarvis_mem0.py --seed    # seed with existing profile + semantic memory
"""

import os
import sys
import json
import argparse
import datetime
from pathlib import Path

import pytz

import config

SCRIPT_DIR = Path(__file__).parent
MEMORY_DIR = SCRIPT_DIR / "memory"
TIMEZONE   = pytz.timezone(config.TIMEZONE)
USER_ID    = "manav"

# ── Mem0 config ───────────────────────────────────────────────────────────────

def _get_mem0_config():
    api_key = config.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in config.py or environment")
    return {
        "llm": {
            "provider": "anthropic",
            "config": {
                "model": "claude-haiku-4-5-20251001",
                "api_key": api_key,
            }
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": "multi-qa-MiniLM-L6-cos-v1"
            }
        },
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "jarvis_memory",
                "path": str(MEMORY_DIR / "mem0_db"),
            }
        },
        "version": "v1.1"
    }


def _get_memory():
    """Returns initialised Mem0 Memory instance."""
    from mem0 import Memory
    return Memory.from_config(_get_mem0_config())


# ── Core operations ───────────────────────────────────────────────────────────

def search_memories(query, limit=5):
    """
    Searches vector memory for the most relevant memories.
    Returns a formatted string for injection into prompts.
    """
    try:
        m       = _get_memory()
        results = m.search(query, filters={"user_id": USER_ID}, limit=limit)
        mems    = results.get("results", [])
        if not mems:
            return ""
        lines = [f"RELEVANT MEMORIES (searched: '{query}'):"]
        for r in mems:
            score = r.get("score", 0)
            if score > 0.5:  # only include highly relevant results
                lines.append(f"  - {r['memory']}")
        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception as e:
        print(f"⚠️  Mem0 search failed: {e}")
        return ""


def get_all_memories():
    """Returns all memories as a formatted string."""
    try:
        m    = _get_memory()
        all_ = m.get_all(filters={"user_id": USER_ID})
        mems = all_.get("results", [])
        if not mems:
            return "No memories stored yet."
        lines = [f"ALL MEMORIES ({len(mems)} total):"]
        for r in mems:
            lines.append(f"  - {r['memory']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Mem0 unavailable: {e}"


def add_memory(text, metadata=None):
    """
    Adds a new memory. Mem0 automatically:
    - Extracts key facts from the text
    - Deduplicates against existing memories
    - Updates contradictory memories
    Returns the result dict.
    """
    try:
        m      = _get_memory()
        meta   = metadata or {"source": "manual", "date": datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")}
        result = m.add(text, user_id=USER_ID, metadata=meta)
        return result
    except Exception as e:
        print(f"⚠️  Mem0 add failed: {e}")
        return {}


def add_memories_from_data(today_data, episodic_entry=""):
    """
    Called nightly by memory_system.py.
    Feeds today's data into Mem0 for extraction and storage.
    Returns number of memories added/updated.
    """
    tz    = TIMEZONE
    today = datetime.datetime.now(tz).strftime("%Y-%m-%d")

    # Build a rich text block for Mem0 to extract facts from
    text_blocks = []

    if episodic_entry and episodic_entry.strip().lower() != "nothing significant.":
        text_blocks.append(f"Today ({today}): {episodic_entry}")

    if today_data:
        text_blocks.append(today_data)

    if not text_blocks:
        return 0

    combined = "\n\n".join(text_blocks)
    meta     = {"source": "nightly_update", "date": today}

    try:
        m      = _get_memory()
        result = m.add(combined, user_id=USER_ID, metadata=meta)
        added  = len(result.get("results", []))
        return added
    except Exception as e:
        print(f"⚠️  Mem0 nightly update failed: {e}")
        return 0


def load_memory_for_prompt(query="", days_back=14):
    """
    Called by morning_brief.py and /ask endpoint.
    Returns a context string with the most relevant memories.

    If query is provided: semantic search for relevant memories.
    If no query: returns recent memories (last N days) from the old system
                 as fallback while Mem0 builds up.
    """
    mem0_context = ""

    # Try Mem0 semantic search
    if query:
        mem0_context = search_memories(query, limit=5)
    else:
        # No query — return top memories broadly relevant to Manav's life
        queries = ["career internship applications", "health fitness sleep", "savings finance"]
        parts   = []
        for q in queries:
            result = search_memories(q, limit=2)
            if result:
                parts.append(result)
        mem0_context = "\n\n".join(parts)

    # Also load the old text-file memory as fallback/supplement
    # (keeps working while Mem0 builds up over time)
    old_context = _load_old_memory(days_back)

    if mem0_context and old_context:
        return f"{mem0_context}\n\n{old_context}"
    return mem0_context or old_context


def _load_old_memory(days_back=14):
    """Loads the existing text-file memory as supplement."""
    sections = []

    semantic_path = MEMORY_DIR / "semantic.md"
    if semantic_path.exists():
        sections.append("SEMANTIC MEMORY (current facts):\n" + semantic_path.read_text())

    episodic_path = MEMORY_DIR / "episodic.md"
    if episodic_path.exists():
        content  = episodic_path.read_text()
        lines    = content.split("\n")
        tz       = TIMEZONE
        cutoff   = datetime.datetime.now(tz).date() - datetime.timedelta(days=days_back)
        recent   = []
        include  = False
        for line in lines:
            if line.startswith("### "):
                try:
                    date_str   = line.replace("### ", "").strip()
                    entry_date = datetime.datetime.strptime(date_str, "%A %d %b %Y").date()
                    include    = entry_date >= cutoff
                except Exception:
                    include = True
            if include:
                recent.append(line)
        if recent:
            sections.append("RECENT HISTORY:\n" + "\n".join(recent))

    return "\n\n".join(sections)


# ── Seed from existing profile + semantic memory ──────────────────────────────

def seed_from_existing():
    """
    One-time operation: seeds Mem0 with the existing profile.md
    and semantic.md so it starts with full context.
    """
    print("🌱  Seeding Mem0 from existing memory files...")

    profile_path  = SCRIPT_DIR / "profile.md"
    semantic_path = MEMORY_DIR / "semantic.md"
    episodic_path = MEMORY_DIR / "episodic.md"

    m = _get_memory()

    # Seed from semantic.md (current facts)
    if semantic_path.exists():
        print("   Adding semantic memory...")
        text   = semantic_path.read_text()
        result = m.add(text, user_id=USER_ID, metadata={"source": "seed_semantic"})
        print(f"   ✅  {len(result.get('results', []))} memories from semantic.md")

    # Seed key sections from profile.md
    if profile_path.exists():
        print("   Adding profile data...")
        text   = profile_path.read_text()
        # Only send the first 3000 chars to avoid token limits
        result = m.add(text[:3000], user_id=USER_ID, metadata={"source": "seed_profile"})
        print(f"   ✅  {len(result.get('results', []))} memories from profile.md")

    # Seed recent episodic entries (last 30 days)
    if episodic_path.exists():
        print("   Adding recent episodic entries...")
        content  = episodic_path.read_text()
        lines    = content.split("\n")
        tz       = TIMEZONE
        cutoff   = datetime.datetime.now(tz).date() - datetime.timedelta(days=30)
        recent   = []
        include  = False
        for line in lines:
            if line.startswith("### "):
                try:
                    date_str   = line.replace("### ", "").strip()
                    entry_date = datetime.datetime.strptime(date_str, "%A %d %b %Y").date()
                    include    = entry_date >= cutoff
                except Exception:
                    include = True
            if include:
                recent.append(line)
        if recent:
            text   = "\n".join(recent)
            result = m.add(text, user_id=USER_ID, metadata={"source": "seed_episodic"})
            print(f"   ✅  {len(result.get('results', []))} memories from episodic.md")

    total = m.get_all(filters={"user_id": USER_ID})
    print(f"\n✅  Seeding complete. Total memories: {len(total.get('results', []))}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis Mem0 Vector Memory")
    parser.add_argument("--search", metavar="QUERY", help="Search memories")
    parser.add_argument("--all",    action="store_true", help="Show all memories")
    parser.add_argument("--add",    metavar="TEXT",  help="Add a memory manually")
    parser.add_argument("--seed",   action="store_true", help="Seed from existing profile + memory files")
    parser.add_argument("--count",  action="store_true", help="Count total memories")
    args = parser.parse_args()

    if args.search:
        print(f"\n🔍  Searching for: '{args.search}'\n")
        result = search_memories(args.search, limit=5)
        print(result or "No relevant memories found.")

    elif args.all:
        print("\n📋  All memories:\n")
        print(get_all_memories())

    elif args.add:
        print(f"\n➕  Adding: '{args.add}'")
        result = add_memory(args.add)
        added  = result.get("results", [])
        for r in added:
            print(f"   {r['event']}: {r['memory']}")
        print(f"✅  Done ({len(added)} memory operations)")

    elif args.seed:
        seed_from_existing()

    elif args.count:
        m     = _get_memory()
        all_  = m.get_all(filters={"user_id": USER_ID})
        count = len(all_.get("results", []))
        print(f"\n📊  Total memories: {count}\n")

    else:
        parser.print_help()
