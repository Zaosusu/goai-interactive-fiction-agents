from app.core.models import WorldAdapter
from app.worlds.sandbox.adapter import SandboxWorldAdapter
from app.worlds.sandbox.store import SandboxWorldStore


def get_world_adapter(world_id: str = "sandbox_1") -> WorldAdapter:
    store = SandboxWorldStore()
    if store.exists(world_id):
        return SandboxWorldAdapter(store.load(world_id))
    raise ValueError(f"Unknown world_id: {world_id}")
