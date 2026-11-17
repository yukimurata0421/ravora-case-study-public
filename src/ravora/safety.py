"""Session and generation fencing for case-study callbacks."""
import uuid

class TurnFence:
    """Reject stale callbacks, including callbacks from another process lifetime."""
    def __init__(self):
        self.session = str(uuid.uuid4())
        self.generation = 0
        self.active = None
        self.played = set()

    def begin(self, turn_id: str) -> dict:
        self.generation += 1
        self.active = turn_id
        return dict(session=self.session, generation=self.generation, turn_id=turn_id)

    def current(self, token: dict) -> bool:
        return token == dict(session=self.session, generation=self.generation, turn_id=self.active)

    def claim_playout(self, token: dict) -> bool:
        key = (token.get("session"), token.get("generation"), token.get("turn_id"))
        if not self.current(token) or key in self.played:
            return False
        self.played.add(key)
        return True

    def cancel(self):
        self.generation += 1
        self.active = None
