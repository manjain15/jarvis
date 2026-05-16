"""
Mem0 test — fixed for v2 API
"""
import os, sys

try:
    from mem0 import Memory
    import chromadb
    print("✅ imports ok")
except ImportError as e:
    print(f"❌ {e}"); sys.exit(1)

api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if not api_key:
    print("❌ ANTHROPIC_API_KEY not set"); sys.exit(1)

config = {
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
            "path": "./memory/mem0_db",
        }
    },
    "version": "v1.1"
}

print("📦  Initialising Mem0...")
m = Memory.from_config(config)
print("✅  Ready")

USER_ID = "manav"

# Add
print("\n➕  Adding memory...")
result = m.add(
    "Manav is saving $2,500/month toward $35,000 for US exchange January 2027",
    user_id=USER_ID
)
print(f"✅  Result: {result}")

# Search — v2 uses filters dict
print("\n🔍  Searching...")
results = m.search("savings goal", filters={"user_id": USER_ID}, limit=3)
print(f"✅  Found {len(results.get('results', []))} results:")
for r in results.get("results", []):
    print(f"   - {r['memory']}")

# Get all
print("\n📋  All memories:")
all_mem = m.get_all(filters={"user_id": USER_ID})
for r in all_mem.get("results", []):
    print(f"   - {r['memory']}")

print("\n✅  Mem0 working correctly.")
