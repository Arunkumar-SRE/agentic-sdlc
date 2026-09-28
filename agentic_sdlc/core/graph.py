"""TaskGraph: a dependency DAG used twice: by the engine for agents, and to validate the plan an agent produces."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .errors import ConfigError


class TaskGraph:
    def __init__(self, deps: Mapping[str, Iterable[str]]) -> None:
        self.deps: dict[str, list[str]] = {node: list(d) for node, d in deps.items()}
        self._validate()
        self.children: dict[str, list[str]] = {n: [] for n in self.deps}
        for node, parents in self.deps.items():
            for parent in parents:
                self.children[parent].append(node)

    @property
    def nodes(self) -> list[str]:
        return list(self.deps)

    def _validate(self) -> None:
        for node, parents in self.deps.items():
            unknown = [p for p in parents if p not in self.deps]
            if unknown:
                raise ConfigError(f"'{node}' depends on unknown node(s): {', '.join(unknown)}")
            if node in parents:
                raise ConfigError(f"'{node}' depends on itself")
        cycle = self._find_cycle()
        if cycle:
            raise ConfigError("dependency cycle (read -> as 'depends on'): " + " -> ".join(cycle))

    def _find_cycle(self) -> list[str] | None:
        colour: dict[str, int] = {n: 0 for n in self.deps}  # 0 new, 1 on stack, 2 done
        stack: list[str] = []

        def visit(node: str) -> list[str] | None:
            colour[node] = 1
            stack.append(node)
            for parent in self.deps[node]:
                if colour[parent] == 1:
                    return stack[stack.index(parent):] + [parent]
                if colour[parent] == 0:
                    found = visit(parent)
                    if found:
                        return found
            stack.pop()
            colour[node] = 2
            return None

        for n in self.deps:
            if colour[n] == 0:
                found = visit(n)
                if found:
                    return found
        return None

    def levels(self) -> list[list[str]]:
        """Kahn's algorithm grouped by depth: nodes in one level can run in parallel."""
        remaining = {n: set(p) for n, p in self.deps.items()}
        out: list[list[str]] = []
        while remaining:
            ready = [n for n, p in remaining.items() if not p]
            out.append(ready)
            for n in ready:
                del remaining[n]
            for p in remaining.values():
                p.difference_update(ready)
        return out

    def order(self) -> list[str]:
        return [n for level in self.levels() for n in level]

    def descendants(self, node: str) -> set[str]:
        seen: set[str] = set()
        frontier = list(self.children[node])
        while frontier:
            n = frontier.pop()
            if n not in seen:
                seen.add(n)
                frontier.extend(self.children[n])
        return seen

    def ancestors(self, node: str) -> set[str]:
        seen: set[str] = set()
        frontier = list(self.deps[node])
        while frontier:
            n = frontier.pop()
            if n not in seen:
                seen.add(n)
                frontier.extend(self.deps[n])
        return seen

    def critical_path(self, weights: Mapping[str, float] | None = None) -> list[str]:
        """Longest weighted chain (default weight 1 per node)."""
        best: dict[str, tuple[float, list[str]]] = {}
        for n in self.order():
            w = (weights or {}).get(n, 1.0)
            prev = max((best[p] for p in self.deps[n]), key=lambda x: x[0], default=(0.0, []))
            best[n] = (prev[0] + w, [*prev[1], n])
        return max(best.values(), key=lambda x: x[0], default=(0.0, []))[1]
