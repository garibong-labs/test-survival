"""Incremental logical counts, independent of physical evidence storage."""
from collections import defaultdict

from ..selection import accounted


def empty():
    return dict(executed=0, carried=0)


class SelectionLedger:
    """Deduplicate phases within the current index; retain only older totals."""
    def __init__(self):
        self.index = None
        self.current = {}
        self.by_index = defaultdict(empty)
        self.by_group = defaultdict(empty)
        self.completed = empty()

    def record(self, index, group, rows):
        if self.index != index:
            if self.index is not None and index < self.index:
                raise ValueError('Selection accounting requires ordered indices')
            self.current.clear()
            self.index = index
        for row in rows:
            action = accounted(row).get('selection_action')
            if action not in self.completed or row.get('state') in ('dead','not-selected'):
                continue
            identity = (group, row['id'])
            prior = self.current.get(identity)
            if prior == 'executed' or prior == action:
                continue
            self.current[identity] = action
            for totals in (self.by_index[index,group], self.by_group[group], self.completed):
                if prior is not None: totals[prior] -= 1
                totals[action] += 1

    def rows(self):
        return [dict(index=index,group=group,**totals) for (index,group),totals in sorted(self.by_index.items())]

    def totals(self, groups):
        return {group:dict(self.by_group.get(group, empty())) for group in groups}
