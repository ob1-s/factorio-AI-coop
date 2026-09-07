"""Session management with conversation tree DAG for save rollback & timeline branching."""
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class TurnNode:
    turn_id: str
    parent_turn_id: Optional[str]
    user_text: str
    assistant_text: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class WorldTimeline:
    def __init__(self, world_id: str):
        self.world_id = world_id
        self.turns: Dict[str, TurnNode] = {}
        self.head_turn_id: str = "turn_0"

    def record_turn(self, turn_id: str, parent_turn_id: Optional[str], user_text: str, context: Optional[dict] = None) -> TurnNode:
        node = TurnNode(
            turn_id=turn_id,
            parent_turn_id=parent_turn_id,
            user_text=user_text,
            context=context or {},
            timestamp=time.time()
        )
        self.turns[turn_id] = node
        return node

    def complete_turn(self, turn_id: str, assistant_text: str) -> Optional[TurnNode]:
        node = self.turns.get(turn_id)
        if node:
            node.assistant_text = assistant_text
            self.head_turn_id = turn_id
        return node

    def get_linear_history(self, head_turn_id: Optional[str] = None, max_turns: int = 12) -> List[Dict[str, str]]:
        """Walk up the parent links from head_turn_id to build the canonical history branch.

        This guarantees that reloaded saves branching from an earlier turn do not
        contain turns from the superseded 'future' timeline.
        """
        curr_id = head_turn_id or self.head_turn_id
        path: List[TurnNode] = []
        visited = set()

        while curr_id and curr_id in self.turns and curr_id not in visited:
            visited.add(curr_id)
            node = self.turns[curr_id]
            if node.assistant_text:  # Only include fully completed turns
                path.append(node)
            curr_id = node.parent_turn_id

        path.reverse()
        if len(path) > max_turns:
            path = path[-max_turns:]

        messages = []
        for node in path:
            messages.append({"role": "user", "content": node.user_text})
            messages.append({"role": "assistant", "content": node.assistant_text})
        return messages


class SessionManager:
    def __init__(self):
        self.worlds: Dict[str, WorldTimeline] = {}
        self.active_world_id: Optional[str] = None

    def get_or_create_world(self, world_id: str) -> WorldTimeline:
        if not world_id:
            world_id = "default_world"
        if world_id not in self.worlds:
            self.worlds[world_id] = WorldTimeline(world_id)
        return self.worlds[world_id]

    def sync_save(self, world_id: str, conversation_head: str) -> WorldTimeline:
        world = self.get_or_create_world(world_id)
        self.active_world_id = world_id
        if conversation_head:
            world.head_turn_id = conversation_head
        return world

    def build_llm_messages(self, world_id: str, parent_turn_id: str, current_user_text: str, context_snapshot: Optional[dict] = None) -> List[Dict[str, str]]:
        world = self.get_or_create_world(world_id)
        history = world.get_linear_history(head_turn_id=parent_turn_id)

        # Append current user turn with grounded context
        current_content = current_user_text
        if context_snapshot:
            import json
            ctx_str = json.dumps(context_snapshot, ensure_ascii=False, indent=1)
            current_content = f"{current_user_text}\n\n[Live Game Context]\n{ctx_str}"

        return history + [{"role": "user", "content": current_content}]
