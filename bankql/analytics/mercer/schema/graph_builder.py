"""FKGraphBuilder — FK relationship graph over a RawSchema.

Nodes   : table names
Edges   : FK relationships (from_table → to_table) with attributes
          {from_col, to_col}

Edges are directed following the FK declaration direction:
  table A has column `b_id` that references table B  →  edge A → B
Both directions are queryable via find_join_path / find_all_paths because
the underlying shortest-path search uses an undirected view of the graph.
"""

from __future__ import annotations

import networkx as nx

from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.core.models import JoinPath, RawSchema

logger = get_logger(__name__)


class FKGraphBuilder:
    """Build and query the FK relationship graph for a RawSchema."""

    def __init__(self, schema: RawSchema) -> None:
        self._schema = schema
        self._graph: nx.DiGraph = nx.DiGraph()
        self._build()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_join_path(
        self, from_table: str, to_table: str
    ) -> list[JoinPath] | None:
        """Return the shortest list of JoinPaths connecting two tables.

        Uses an undirected view so the path is found regardless of FK
        direction.  Returns None if the tables are disconnected or either
        table is absent.
        """
        if from_table not in self._graph or to_table not in self._graph:
            return None
        if from_table == to_table:
            return []

        undirected = self._graph.to_undirected(as_view=True)
        try:
            node_path: list[str] = nx.shortest_path(
                undirected, source=from_table, target=to_table
            )
        except nx.NetworkXNoPath:
            return None

        return self._path_to_join_paths(node_path)

    def find_all_paths(self, tables: list[str]) -> list[JoinPath]:
        """Return the minimal deduplicated set of JoinPaths to connect all tables.

        Algorithm: build a complete graph of pairwise shortest-path distances
        among the requested tables, then find a minimum spanning tree over
        that distance graph.  Walk the MST edges and collect the FK hops.
        """
        unique_tables = list(dict.fromkeys(tables))  # dedup, preserve order
        if len(unique_tables) <= 1:
            return []

        undirected = self._graph.to_undirected(as_view=True)

        # Only keep tables that exist in the graph
        present = [t for t in unique_tables if t in self._graph]
        if len(present) <= 1:
            return []

        # Build a distance/path cache between all pairs in `present`
        pair_paths: dict[tuple[str, str], list[str]] = {}
        for i, src in enumerate(present):
            for dst in present[i + 1 :]:
                try:
                    p = nx.shortest_path(undirected, source=src, target=dst)
                    pair_paths[(src, dst)] = p
                except nx.NetworkXNoPath:
                    pass  # disconnected pair — omit from MST

        if not pair_paths:
            return []

        # Build a weighted graph of pairs, then find the MST
        pair_graph: nx.Graph = nx.Graph()
        pair_graph.add_nodes_from(present)
        for (src, dst), path in pair_paths.items():
            pair_graph.add_edge(src, dst, weight=len(path) - 1, path=path)

        mst = nx.minimum_spanning_tree(pair_graph, weight="weight")

        # Collect all FK hops across MST edges, deduplicating by (from, col, to)
        seen: set[tuple[str, str, str, str]] = set()
        result: list[JoinPath] = []
        for src, dst in mst.edges():
            hop_path: list[str] = pair_graph[src][dst]["path"]
            for jp in self._path_to_join_paths(hop_path):
                key = (jp.from_table, jp.from_column, jp.to_table, jp.to_column)
                rev_key = (jp.to_table, jp.to_column, jp.from_table, jp.from_column)
                if key not in seen and rev_key not in seen:
                    seen.add(key)
                    result.append(jp)

        return result

    def get_neighbors(self, table: str) -> list[str]:
        """Return tables directly connected by one FK hop (either direction)."""
        if table not in self._graph:
            return []
        undirected = self._graph.to_undirected(as_view=True)
        return list(undirected.neighbors(table))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build(self) -> None:
        """Populate the DiGraph from the schema's FK relationships."""
        for table in self._schema.tables:
            self._graph.add_node(table.name)

        edges_added = 0
        for table in self._schema.tables:
            for col in table.columns:
                if col.is_foreign_key and col.references_table:
                    # Ensure the referenced table node exists even if absent from schema
                    if col.references_table not in self._graph:
                        self._graph.add_node(col.references_table)
                    self._graph.add_edge(
                        table.name,
                        col.references_table,
                        from_col=col.name,
                        to_col=col.references_column or col.name,
                    )
                    edges_added += 1

        logger.debug(
            "fk_graph_built",
            tables=self._graph.number_of_nodes(),
            edges=edges_added,
        )

    def _path_to_join_paths(self, node_path: list[str]) -> list[JoinPath]:
        """Convert a list of table names into JoinPath objects.

        For each consecutive pair (A, B) we look for a directed edge A→B or
        B→A in the DiGraph and extract the FK column attributes.
        """
        join_paths: list[JoinPath] = []
        for i in range(len(node_path) - 1):
            a, b = node_path[i], node_path[i + 1]

            if self._graph.has_edge(a, b):
                attrs = self._graph[a][b]
                join_paths.append(
                    JoinPath(
                        from_table=a,
                        from_column=attrs["from_col"],
                        to_table=b,
                        to_column=attrs["to_col"],
                    )
                )
            elif self._graph.has_edge(b, a):
                # Traverse in reverse — FK declared on b points to a
                attrs = self._graph[b][a]
                join_paths.append(
                    JoinPath(
                        from_table=a,
                        from_column=attrs["to_col"],
                        to_table=b,
                        to_column=attrs["from_col"],
                    )
                )
            # else: no direct edge (shouldn't happen if the path came from nx)

        return join_paths
