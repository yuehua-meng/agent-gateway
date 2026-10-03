"""Single-event-loop admission: shared text slots, bounded FIFO image waiting."""

import asyncio
from collections import deque
from dataclasses import dataclass


class CapacityFull(Exception):
    pass


@dataclass(eq=False)
class Ticket:
    kind: str
    ready: asyncio.Future
    active: bool = False
    released: bool = False


class Admission:
    def __init__(self, text_limit, image_limit, image_queue_limit):
        self.limits = {"chat": text_limit, "image": image_limit}
        self.active = {"chat": 0, "image": 0}
        self.waiting = deque()
        self.image_queue_limit = image_queue_limit

    def reserve(self, kind):
        # No await between checking and reserving; callers run on the same event loop.
        free = self.active[kind] < self.limits[kind]
        if not free and (kind == "chat" or len(self.waiting) >= self.image_queue_limit):
            raise CapacityFull()
        ticket = Ticket(kind, asyncio.get_running_loop().create_future())
        if free:
            self.active[kind] += 1
            ticket.active = True
            ticket.ready.set_result(None)
        else:
            self.waiting.append(ticket)
        return ticket

    def release(self, ticket):
        if ticket.released:
            return
        ticket.released = True
        if not ticket.active:
            self.waiting.remove(ticket)
            ticket.ready.cancel()
            return
        self.active[ticket.kind] -= 1
        if ticket.kind == "image" and self.waiting:
            next_ticket = self.waiting.popleft()
            next_ticket.active = True
            self.active["image"] += 1
            next_ticket.ready.set_result(None)

    def snapshot(self):
        return {"text_active": self.active["chat"], "text_limit": self.limits["chat"],
                "image_active": self.active["image"], "image_limit": self.limits["image"],
                "image_waiting": len(self.waiting), "image_queue_limit": self.image_queue_limit}
