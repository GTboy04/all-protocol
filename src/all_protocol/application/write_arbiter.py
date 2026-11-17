"""Address-span ownership shared by base simulation and conditional scenarios."""


class WriteArbiter:
    def __init__(self, points):
        self.spans = {point.target: point.span for point in points}
        self.manual = set()
        self.owners = {}

    def claim(self, owner, targets):
        if any(target in self.manual or self.owners.get(target, owner) != owner for target in targets):
            return False
        self.owners.update((target, owner) for target in targets)
        return True

    def take_over(self, span):
        targets = {target for target, covered in self.spans.items() if covered.overlaps(span)}
        self.manual.update(targets)
        return {self.owners[target] for target in targets if target in self.owners}

    def release(self, owner):
        released = {target for target, current in self.owners.items() if current == owner}
        for target in released:
            del self.owners[target]
        return released

    def reset(self):
        self.manual.clear()
        self.owners.clear()
