"""
Knowledge Graph Context Database Layer
Provides SQLite-based persistence for a knowledge graph with nodes and edges.
"""

import sqlite3
import json
import os
import sys
import argparse
import random
import re
import math
import hashlib
from collections import defaultdict
from datetime import datetime
from urllib.error import URLError
from urllib.request import Request, urlopen


class Database:
    """SQLite database for knowledge graph context."""

    def __init__(self, db_path=None):
        """Initialize database connection and create tables if needed.

        Args:
            db_path: Path to SQLite database file. If None, checks the CONTEXT_DB
                environment variable, then falls back to context.db in the script
                directory. The CONTEXT_DB override exists so that callers running
                from a FUSE/virtiofs-mounted filesystem (where SQLite writes fail
                with 'disk I/O error' due to imperfect POSIX lock/fsync support)
                can redirect SQLite operations to a native filesystem while the
                canonical DB continues to live alongside this script.
        """
        if db_path is None:
            env_path = os.environ.get('CONTEXT_DB')
            if env_path:
                db_path = env_path
            else:
                # Resolve path relative to this script's location
                script_dir = os.path.dirname(os.path.abspath(__file__))
                db_path = os.path.join(script_dir, 'context.db')

        self.db_path = db_path
        # timeout: wait up to 30s for any transient lock contention before raising
        self.conn = sqlite3.connect(self.db_path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # Safe-mode pragmas: cheap durability improvements that help on any
        # filesystem and are especially useful if this ever runs against a
        # less-than-ideal mount. busy_timeout gives concurrent readers/writers
        # room to retry; synchronous=NORMAL is the standard recommendation for
        # write-heavy workloads without losing crash safety on a real FS.
        self.conn.execute('PRAGMA busy_timeout = 30000')
        self.conn.execute('PRAGMA synchronous = NORMAL')
        self._create_tables()

    def _create_tables(self):
        """Create tables and indexes if they don't exist."""
        cursor = self.conn.cursor()

        # Create nodes table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                body TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT,
                updated_at TEXT
            )
        ''')

        # Create edges table with foreign key constraints
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_node TEXT NOT NULL,
                to_node TEXT NOT NULL,
                relationship TEXT NOT NULL,
                context TEXT,
                created_at TEXT,
                FOREIGN KEY (from_node) REFERENCES nodes(id),
                FOREIGN KEY (to_node) REFERENCES nodes(id)
            )
        ''')

        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_node)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_node)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_relationship ON edges(relationship)')

        # Create confidence cache table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS confidence_cache (
                node_id TEXT PRIMARY KEY,
                confidence REAL NOT NULL,
                inferred_type TEXT NOT NULL,
                frequency_component REAL,
                support_component REAL,
                competition_component REAL,
                graph_hash TEXT NOT NULL,
                computed_at TEXT NOT NULL,
                FOREIGN KEY (node_id) REFERENCES nodes(id)
            )
        ''')

        self.conn.commit()

    def add_node(self, id, type, name, body=None, metadata=None):
        """Add a new node to the graph.

        Args:
            id: Unique identifier for the node
            type: Node type (e.g., 'idea', 'decision', 'metric')
            name: Human-readable name
            body: Optional content/description
            metadata: Optional dict of additional fields (stored as JSON)

        Returns:
            True if successful
        """
        if metadata is None:
            metadata = {}

        now = datetime.utcnow().isoformat()
        metadata_json = json.dumps(metadata) if isinstance(metadata, dict) else metadata

        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO nodes (id, type, name, body, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (id, type, name, body, metadata_json, now, now))

        self.conn.commit()
        return True

    def update_node(self, id, **fields):
        """Update node fields.

        Args:
            id: Node identifier
            **fields: Field names and values to update (e.g., name="New Name", body="...")

        Returns:
            True if successful
        """
        if not fields:
            return True

        # Handle metadata specially if it's a dict
        if 'metadata' in fields and isinstance(fields['metadata'], dict):
            fields['metadata'] = json.dumps(fields['metadata'])

        # Add updated_at
        fields['updated_at'] = datetime.utcnow().isoformat()

        # Build SET clause dynamically
        set_clause = ', '.join(f'{k} = ?' for k in fields.keys())
        values = list(fields.values()) + [id]

        cursor = self.conn.cursor()
        cursor.execute(f'UPDATE nodes SET {set_clause} WHERE id = ?', values)
        self.conn.commit()
        return True

    def get_node(self, id):
        """Retrieve a node by ID.

        Args:
            id: Node identifier

        Returns:
            Dict representation of node, or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM nodes WHERE id = ?', (id,))
        row = cursor.fetchone()

        if row:
            # Convert row to dict and parse metadata
            node = dict(row)
            if node['metadata']:
                node['metadata'] = json.loads(node['metadata'])
            return node
        return None

    def add_edge(self, from_node, to_node, relationship, context=None):
        """Add a relationship edge between two nodes.

        Args:
            from_node: Source node ID
            to_node: Target node ID
            relationship: Type of relationship (e.g., 'depends_on', 'related_to')
            context: Optional context/description for the relationship

        Returns:
            Edge ID if successful
        """
        now = datetime.utcnow().isoformat()

        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO edges (from_node, to_node, relationship, context, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (from_node, to_node, relationship, context, now))

        self.conn.commit()
        return cursor.lastrowid

    def get_edges(self, node_id, direction='both', relationship_filter=None):
        """Retrieve edges connected to a node.

        Args:
            node_id: Node identifier
            direction: 'outgoing' (from this node), 'incoming' (to this node), or 'both'
            relationship_filter: Optional string or list of relationship types to filter

        Returns:
            List of edge dicts
        """
        cursor = self.conn.cursor()

        # Build WHERE clause based on direction
        if direction == 'outgoing':
            where = 'from_node = ?'
            params = [node_id]
        elif direction == 'incoming':
            where = 'to_node = ?'
            params = [node_id]
        elif direction == 'both':
            where = '(from_node = ? OR to_node = ?)'
            params = [node_id, node_id]
        else:
            raise ValueError(f"Invalid direction: {direction}")

        # Add relationship filter if specified
        if relationship_filter:
            if isinstance(relationship_filter, str):
                where += ' AND relationship = ?'
                params.append(relationship_filter)
            elif isinstance(relationship_filter, (list, tuple)):
                placeholders = ','.join('?' * len(relationship_filter))
                where += f' AND relationship IN ({placeholders})'
                params.extend(relationship_filter)

        cursor.execute(f'SELECT * FROM edges WHERE {where}', params)
        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def query(self, sql, params=None):
        """Execute arbitrary SQL query.

        Args:
            sql: SQL query string
            params: Optional parameters for parameterized query

        Returns:
            List of tuples (fetchall result)
        """
        cursor = self.conn.cursor()
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        return cursor.fetchall()

    def get_neighbors(self, node_id, hops=2, relationship_filter=None):
        """BFS traversal from a starting node to discover connections within N hops.

        Args:
            node_id: Starting node ID
            hops: Number of hops to traverse (default 2)
            relationship_filter: Optional string or list of relationship types to filter

        Returns:
            List of dicts: {"node": node_dict, "edge": edge_dict, "depth": int}
            Traverses in both directions for all edge types to discover connections regardless of direction.
        """
        cursor = self.conn.cursor()
        visited = set()
        results = []
        queue = [(node_id, 0)]  # (node_id, current_depth)

        while queue:
            current_node_id, current_depth = queue.pop(0)

            if current_node_id in visited:
                continue

            visited.add(current_node_id)

            # If we're past the starting node, fetch and add to results
            if current_depth > 0:
                node = self.get_node(current_node_id)
                if node:
                    # Find the edge that led to this node
                    edges = self.get_edges(current_node_id, direction='both', relationship_filter=relationship_filter)
                    for edge in edges:
                        # Only include edges where we arrived from a visited node
                        parent_id = edge['from_node'] if edge['to_node'] == current_node_id else edge['to_node']
                        if parent_id in visited and parent_id != current_node_id:
                            results.append({
                                'node': node,
                                'edge': edge,
                                'depth': current_depth
                            })
                            break

            # Continue traversal if within hop limit
            if current_depth < hops:
                edges = self.get_edges(current_node_id, direction='both', relationship_filter=relationship_filter)
                for edge in edges:
                    # Determine the other end of the edge
                    next_node_id = edge['to_node'] if edge['from_node'] == current_node_id else edge['from_node']

                    if next_node_id not in visited:
                        queue.append((next_node_id, current_depth + 1))

        return results

    def find_nodes(self, search_term, type_filter=None):
        """Fuzzy search for nodes by name and body using SQL LIKE.

        Args:
            search_term: Term to search for (case-insensitive)
            type_filter: Optional node type to filter results

        Returns:
            List of matching node dicts
        """
        cursor = self.conn.cursor()

        if type_filter:
            cursor.execute('''
                SELECT * FROM nodes
                WHERE type = ? AND (name LIKE ? OR body LIKE ?)
                ORDER BY name
            ''', (type_filter, f'%{search_term}%', f'%{search_term}%'))
        else:
            cursor.execute('''
                SELECT * FROM nodes
                WHERE name LIKE ? OR body LIKE ?
                ORDER BY name
            ''', (f'%{search_term}%', f'%{search_term}%'))

        rows = cursor.fetchall()
        results = []

        for row in rows:
            node = dict(row)
            if node['metadata']:
                node['metadata'] = json.loads(node['metadata'])
            results.append(node)

        return results

    def get_recent_sessions(self, count=14):
        """Retrieve the N most recent session nodes with their connected threads.

        Args:
            count: Number of recent sessions to retrieve (default 14)

        Returns:
            List of dicts: {"session": node_dict, "threads": [{"thread": node_dict, "what_changed": edge_context}]}
            Sessions are ordered by created_at DESC, and threads are connected via ADVANCED edges.
        """
        cursor = self.conn.cursor()

        # Get the most recent session nodes
        cursor.execute('''
            SELECT * FROM nodes
            WHERE type = 'session'
            ORDER BY created_at DESC
            LIMIT ?
        ''', (count,))

        session_rows = cursor.fetchall()
        results = []

        for session_row in session_rows:
            session = dict(session_row)
            if session['metadata']:
                session['metadata'] = json.loads(session['metadata'])

            # Find all ADVANCED edges connected to this session
            cursor.execute('''
                SELECT * FROM edges
                WHERE (from_node = ? OR to_node = ?) AND relationship = 'ADVANCED'
            ''', (session['id'], session['id']))

            edge_rows = cursor.fetchall()
            threads = []

            for edge_row in edge_rows:
                edge = dict(edge_row)

                # Determine which end is the thread node
                thread_id = edge['to_node'] if edge['from_node'] == session['id'] else edge['from_node']
                thread_node = self.get_node(thread_id)

                if thread_node:
                    threads.append({
                        'thread': thread_node,
                        'what_changed': edge['context']
                    })

            results.append({
                'session': session,
                'threads': threads
            })

        return results

    def graph_hash(self):
        """Compute a hash of the current graph state for cache invalidation.

        Returns:
            String hash: "{node_count}:{edge_count}:{latest_updated_at}"
        """
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM nodes')
        node_count = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM edges')
        edge_count = cursor.fetchone()[0]

        cursor.execute('SELECT MAX(updated_at) FROM nodes')
        latest = cursor.fetchone()[0] or ''

        return f"{node_count}:{edge_count}:{latest}"

    def close(self):
        """Close database connection."""
        self.conn.close()


class ConfidenceEngine:
    """Computes confidence scores for knowledge graph nodes.

    Confidence is a float 0-1 per node, computed from:
    - Frequency: unique sessions x log-weighted total touches
    - Support: confidence of connected nodes via RELATES_TO
    - Competition: EVOLVED_FROM forks within the same thread

    No time-based decay. Scores change only when the graph changes.
    """

    # Fixed confidence values by inferred type
    FIXED_SCORES = {
        'settled_fact': 0.9,
        'stable_source': 0.7,
    }

    # Floors for dynamic types (uncontested minimum)
    FLOORS = {
        'active_direction': 0.5,
        'open_question': 0.3,
        'superseded': 0.1,
    }

    # Formula weights
    W_FREQUENCY = 0.5
    W_SUPPORT = 0.3
    W_COMPETITION = 0.2

    def __init__(self, db):
        """Initialize with a Database instance."""
        self.db = db

    def infer_type(self, node_id):
        """Infer a node's behavioral type from graph structure.

        Returns one of: settled_fact, active_direction, superseded,
                        open_question, stable_source, thread_rollup, session
        """
        node = self.db.get_node(node_id)
        if not node:
            return 'unknown'

        node_type = node['type']

        # Source nodes are always stable
        if node_type == 'source':
            return 'stable_source'

        # Session nodes — not scored meaningfully
        if node_type == 'session':
            return 'session'

        # Question nodes — check if resolved
        if node_type == 'question':
            resolved_edges = self.db.get_edges(node_id, direction='outgoing',
                                                relationship_filter='RESOLVED_BY')
            if resolved_edges:
                return 'settled_fact'
            return 'open_question'

        # Thread nodes — rollup of children
        if node_type == 'thread':
            return 'thread_rollup'

        # Concept nodes — check if superseded
        if node_type == 'concept':
            # Is this node the OLD side of an EVOLVED_FROM edge?
            # EVOLVED_FROM goes from new -> old, so check incoming
            evolved_from_incoming = self.db.get_edges(
                node_id, direction='incoming',
                relationship_filter='EVOLVED_FROM'
            )
            # Also check if this is labeled as "old" in the node id
            is_old = 'old-' in node_id

            if evolved_from_incoming or is_old:
                # Check if replacement was itself superseded (idea came back)
                if evolved_from_incoming:
                    replacement_id = evolved_from_incoming[0]['from_node']
                    replacement_superseded = self.db.get_edges(
                        replacement_id, direction='incoming',
                        relationship_filter='EVOLVED_FROM'
                    )
                    if replacement_superseded:
                        return 'active_direction'  # Idea came back
                return 'superseded'

            return 'active_direction'

        return 'unknown'

    def _compute_frequency(self, thread_id):
        """Compute frequency score for a thread.

        frequency = unique_sessions x (1 + log(total_touches / unique_sessions))
        Normalized 0-1 against the max frequency across all threads.

        Args:
            thread_id: Thread node ID

        Returns:
            Raw (unnormalized) frequency value
        """
        cursor = self.db.conn.cursor()

        # Count unique sessions that touched this thread via ADVANCED edges
        cursor.execute('''
            SELECT COUNT(DISTINCT e.from_node) as unique_sessions,
                   COUNT(*) as total_touches
            FROM edges e
            JOIN nodes n ON e.from_node = n.id
            WHERE e.to_node = ?
              AND e.relationship = 'ADVANCED'
              AND n.type = 'session'
        ''', (thread_id,))

        row = cursor.fetchone()
        unique_sessions = row[0] or 0
        total_touches = row[1] or 0

        # Also count RAISED_IN edges (questions raised against this thread)
        cursor.execute('''
            SELECT COUNT(DISTINCT e.from_node) as unique_sessions_q,
                   COUNT(*) as total_touches_q
            FROM edges e
            JOIN nodes n_from ON e.from_node = n_from.id
            WHERE e.to_node = ?
              AND e.relationship = 'RAISED_IN'
        ''', (thread_id,))

        row_q = cursor.fetchone()
        unique_sessions += row_q[0] or 0
        total_touches += row_q[1] or 0

        if unique_sessions == 0:
            return 0.0

        # frequency = unique_sessions x (1 + log(total_touches / unique_sessions))
        ratio = total_touches / unique_sessions
        frequency = unique_sessions * (1 + math.log(max(ratio, 1)))

        return frequency

    def _get_all_thread_frequencies(self):
        """Compute raw frequency for all threads. Returns dict {thread_id: raw_freq}."""
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT id FROM nodes WHERE type = 'thread'")
        threads = [row[0] for row in cursor.fetchall()]

        freqs = {}
        for tid in threads:
            freqs[tid] = self._compute_frequency(tid)

        return freqs

    def _compute_competition(self, node_id, thread_id):
        """Compute competition penalty for a concept node on a thread.

        Detects EVOLVED_FROM forks: if a common ancestor has multiple
        replacement branches, they compete. The branch with more downstream
        support wins; others get penalized.

        Args:
            node_id: The concept node to evaluate
            thread_id: The thread this node belongs to

        Returns:
            Float 0-1. 0 = uncontested, 1 = fully outcompeted.
        """
        # Check if something replaced this node (incoming EVOLVED_FROM)
        incoming = self.db.get_edges(node_id, direction='incoming',
                                      relationship_filter='EVOLVED_FROM')

        if not incoming:
            # Nothing replaced this node — uncontested
            return 0.0

        # This node was superseded. Competition penalty based on how strong
        # the replacement is relative to this node.
        replacement_id = incoming[0]['from_node']
        replacement_also_superseded = self.db.get_edges(
            replacement_id, direction='incoming',
            relationship_filter='EVOLVED_FROM'
        )

        if replacement_also_superseded:
            return 0.2  # Mild penalty — the thing that replaced us also got replaced
        return 0.8  # Strong penalty — we were superseded and replacement stands

    def _compute_support(self, node_id, scores_so_far):
        """Compute support score from connected nodes via RELATES_TO.

        1-hop connections weighted at 1.0, 2-hop at 0.5.
        Only RELATES_TO edges propagate support.

        Args:
            node_id: Node to compute support for
            scores_so_far: Dict of {node_id: confidence} computed so far
                           (used for iterative propagation)

        Returns:
            Float 0-1 representing support from the neighborhood
        """
        neighbors = self.db.get_neighbors(node_id, hops=2,
                                           relationship_filter='RELATES_TO')

        if not neighbors:
            return 0.0

        weighted_sum = 0.0
        total_weight = 0.0

        for neighbor in neighbors:
            nid = neighbor['node']['id']
            depth = neighbor['depth']

            # Skip self
            if nid == node_id:
                continue

            # Weight: 1.0 for 1-hop, 0.5 for 2-hop
            weight = 1.0 if depth == 1 else 0.5

            # Use score from scores_so_far if available, else use type-based default
            if nid in scores_so_far:
                score = scores_so_far[nid]
            else:
                inferred = self.infer_type(nid)
                score = self.FIXED_SCORES.get(inferred, 0.5)

            weighted_sum += weight * score
            total_weight += weight

        if total_weight == 0:
            return 0.0

        return weighted_sum / total_weight

    def _get_cached_scores(self):
        """Return cached scores if graph hasn't changed, else None."""
        if os.environ.get('CONTEXT_DISABLE_CONFIDENCE_CACHE') == '1':
            return None

        current_hash = self.db.graph_hash()
        cursor = self.db.conn.cursor()

        cursor.execute('SELECT graph_hash FROM confidence_cache LIMIT 1')
        row = cursor.fetchone()

        if row and row[0] == current_hash:
            # Cache is valid
            cursor.execute('SELECT * FROM confidence_cache')
            scores = {}
            for r in cursor.fetchall():
                scores[r['node_id']] = {
                    'confidence': r['confidence'],
                    'inferred_type': r['inferred_type'],
                    'frequency_component': r['frequency_component'],
                    'support_component': r['support_component'],
                    'competition_component': r['competition_component'],
                }
            return scores

        return None

    def _save_cache(self, scores):
        """Save computed scores to cache."""
        if os.environ.get('CONTEXT_DISABLE_CONFIDENCE_CACHE') == '1':
            return

        current_hash = self.db.graph_hash()
        now = datetime.utcnow().isoformat()
        cursor = self.db.conn.cursor()

        # Clear old cache
        cursor.execute('DELETE FROM confidence_cache')

        for node_id, data in scores.items():
            cursor.execute('''
                INSERT INTO confidence_cache
                (node_id, confidence, inferred_type, frequency_component,
                 support_component, competition_component, graph_hash, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                node_id, data['confidence'], data['inferred_type'],
                data.get('frequency_component'), data.get('support_component'),
                data.get('competition_component'), current_hash, now
            ))

        self.db.conn.commit()

    def compute_all(self):
        """Compute confidence scores for all nodes.

        Uses cache if graph hasn't changed. Otherwise recomputes everything.

        Returns:
            Dict of {node_id: {confidence, inferred_type, frequency_component,
                               support_component, competition_component}}
        """
        # Check cache first
        cached = self._get_cached_scores()
        if cached is not None:
            return cached

        cursor = self.db.conn.cursor()
        cursor.execute('SELECT id FROM nodes')
        all_nodes = [row[0] for row in cursor.fetchall()]

        # Phase 1: Infer all types
        types = {}
        for nid in all_nodes:
            types[nid] = self.infer_type(nid)

        # Phase 2: Compute frequency for all threads
        thread_freqs = self._get_all_thread_frequencies()
        max_freq = max(thread_freqs.values()) if thread_freqs else 1.0
        if max_freq == 0:
            max_freq = 1.0

        # Phase 3: First pass — assign initial scores
        scores = {}
        for nid in all_nodes:
            inferred = types[nid]

            if inferred in self.FIXED_SCORES:
                scores[nid] = {
                    'confidence': self.FIXED_SCORES[inferred],
                    'inferred_type': inferred,
                    'frequency_component': None,
                    'support_component': None,
                    'competition_component': None,
                }
                continue

            if inferred == 'session':
                scores[nid] = {
                    'confidence': 0.5,
                    'inferred_type': 'session',
                    'frequency_component': None,
                    'support_component': None,
                    'competition_component': None,
                }
                continue

            if inferred == 'superseded':
                competition = self._compute_competition(nid, '')
                base = 0.15 + (0.15 * (1 - competition))
                scores[nid] = {
                    'confidence': base,
                    'inferred_type': inferred,
                    'frequency_component': None,
                    'support_component': None,
                    'competition_component': competition,
                }
                continue

            if inferred == 'open_question':
                # Will be refined in Phase 4 with parent thread confidence
                scores[nid] = {
                    'confidence': 0.3,
                    'inferred_type': inferred,
                    'frequency_component': None,
                    'support_component': None,
                    'competition_component': None,
                }
                continue

            # Active direction or thread_rollup — needs full formula
            # Find which thread this node belongs to
            thread_id = nid if types[nid] == 'thread_rollup' else None
            if thread_id is None:
                # Find parent thread via edges
                edges = self.db.get_edges(nid, direction='both',
                                          relationship_filter=['ADVANCED', 'RELATES_TO'])
                for e in edges:
                    other = e['to_node'] if e['from_node'] == nid else e['from_node']
                    if other.startswith('thread:'):
                        thread_id = other
                        break

            # Frequency (from parent thread)
            freq_raw = thread_freqs.get(thread_id, 0) if thread_id else 0
            norm_freq = freq_raw / max_freq

            # Competition
            competition = self._compute_competition(nid, thread_id or '')

            # Placeholder support — will be computed in Phase 4
            scores[nid] = {
                'confidence': 0.5,  # Temporary
                'inferred_type': inferred,
                'frequency_component': norm_freq,
                'support_component': 0.0,
                'competition_component': competition,
            }

        # Phase 4: Iterative support propagation (2 passes)
        for iteration in range(2):
            current_scores = {nid: data['confidence'] for nid, data in scores.items()}

            for nid in all_nodes:
                inferred = types[nid]

                if inferred in ('active_direction', 'thread_rollup'):
                    support = self._compute_support(nid, current_scores)
                    scores[nid]['support_component'] = support

                    freq = scores[nid]['frequency_component'] or 0
                    comp = scores[nid]['competition_component'] or 0
                    floor = self.FLOORS.get(inferred, 0.5)

                    raw = (self.W_FREQUENCY * freq +
                           self.W_SUPPORT * support +
                           self.W_COMPETITION * (1 - comp))

                    # Apply uncontested floor
                    if comp == 0:
                        scores[nid]['confidence'] = max(raw, floor)
                    else:
                        scores[nid]['confidence'] = raw

                elif inferred == 'open_question':
                    # Boost by parent thread confidence
                    edges = self.db.get_edges(nid, direction='both',
                                              relationship_filter='RAISED_IN')
                    parent_conf = 0.5
                    for e in edges:
                        other = e['to_node'] if e['from_node'] == nid else e['from_node']
                        if other in current_scores:
                            parent_conf = max(parent_conf, current_scores[other])

                    scores[nid]['confidence'] = 0.3 + (0.2 * parent_conf)

        # Phase 5: Thread rollup — average of child concept confidence
        for nid in all_nodes:
            if types[nid] == 'thread_rollup':
                edges = self.db.get_edges(nid, direction='both')
                child_scores = []
                for e in edges:
                    other = e['to_node'] if e['from_node'] == nid else e['from_node']
                    if other in scores and types.get(other) in ('active_direction', 'settled_fact'):
                        child_scores.append(scores[other]['confidence'])

                if child_scores:
                    # Use max of: thread's own computed score, weighted avg of children
                    child_avg = sum(child_scores) / len(child_scores)
                    own_score = scores[nid]['confidence']
                    scores[nid]['confidence'] = max(own_score, child_avg)

        # Save to cache
        self._save_cache(scores)

        return scores

    def get_score(self, node_id):
        """Get confidence score for a single node.

        Returns:
            Dict with confidence, inferred_type, and component breakdown.
            Or None if node not found.
        """
        all_scores = self.compute_all()
        return all_scores.get(node_id)


def slugify(text):
    """Convert text to slug format.

    Rules:
    - Lowercase
    - Replace spaces with hyphens
    - Remove non-alphanumeric except hyphens
    - Truncate to 50 chars

    Args:
        text: Input string

    Returns:
        Slugified string
    """
    # Lowercase
    slug = text.lower()
    # Replace spaces with hyphens
    slug = slug.replace(' ', '-')
    # Remove non-alphanumeric except hyphens
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    # Clean up multiple consecutive hyphens
    slug = re.sub(r'-+', '-', slug)
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    # Truncate to 50 chars
    slug = slug[:50]
    return slug


def get_state_file_path():
    """Get the path to the state file (.context_session).

    Returns:
        Absolute path to state file in same directory as context.py
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, '.context_session')


def get_active_session():
    """Read the active session ID from .context_session file.

    Returns:
        Session ID string if found, otherwise None
    """
    state_file = get_state_file_path()
    if os.path.exists(state_file):
        with open(state_file, 'r') as f:
            session_id = f.read().strip()
            return session_id if session_id else None
    return None


def save_active_session(session_id):
    """Write the active session ID to .context_session file.

    Args:
        session_id: Session ID to save
    """
    state_file = get_state_file_path()
    with open(state_file, 'w') as f:
        f.write(session_id)


def random_hex(length=4):
    """Generate a random hex string of specified length.

    Args:
        length: Number of hex characters

    Returns:
        Random hex string
    """
    return ''.join(random.choices('0123456789abcdef', k=length))


def get_today():
    """Get today's date in YYYY-MM-DD format.

    Returns:
        Date string
    """
    return datetime.utcnow().strftime('%Y-%m-%d')


def get_script_dir():
    """Return the directory containing context.py."""
    return os.path.dirname(os.path.abspath(__file__))


def get_vault_base():
    """Return the Obsidian vault base path."""
    env_path = (
        os.environ.get('CONTEXT_VAULT_PATH')
        or os.environ.get('CONTEXT_VAULT')
        or os.environ.get('OBSIDIAN_VAULT_PATH')
    )
    if env_path:
        return os.path.abspath(os.path.expanduser(env_path))

    script_dir = get_script_dir()
    cowork_vault = os.path.join(script_dir, 'Startup', 'Daily notes', 'Vault')
    if os.path.isdir(cowork_vault):
        return cowork_vault

    return os.path.join(script_dir, 'Vault')


def get_memory_root():
    """Return the Karpathy-style LLM Wiki root path."""
    env_path = os.environ.get('CONTEXT_MEMORY_ROOT')
    if env_path:
        return os.path.abspath(os.path.expanduser(env_path))
    return os.path.join(get_vault_base(), '11-LLM-Wiki')


def get_memory_paths():
    """Return canonical paths used by the LLM Wiki layer."""
    root = get_memory_root()
    return {
        'root': root,
        'raw': os.path.join(root, 'raw'),
        'raw_chats': os.path.join(root, 'raw', 'chats'),
        'raw_sources': os.path.join(root, 'raw', 'sources'),
        'raw_assets': os.path.join(root, 'raw', 'assets'),
        'wiki': os.path.join(root, 'wiki'),
        'wiki_threads': os.path.join(root, 'wiki', 'threads'),
        'wiki_sources': os.path.join(root, 'wiki', 'sources'),
        'wiki_synthesis': os.path.join(root, 'wiki', 'synthesis'),
        'schema': os.path.join(root, 'schema.md'),
        'index': os.path.join(root, 'wiki', 'index.md'),
        'log': os.path.join(root, 'wiki', 'log.md'),
        'current_state': os.path.join(root, 'wiki', 'current-state.md'),
        'discarded': os.path.join(root, 'wiki', 'discarded-ideas.md'),
        'open_questions': os.path.join(root, 'wiki', 'open-questions.md'),
        'source_ledger': os.path.join(root, 'wiki', 'source-ledger.md'),
        'session_ledger': os.path.join(root, 'wiki', 'session-ledger.md'),
    }


def ensure_memory_dirs():
    """Create the LLM Wiki directory skeleton if missing."""
    paths = get_memory_paths()
    for key in (
        'root', 'raw', 'raw_chats', 'raw_sources', 'raw_assets',
        'wiki', 'wiki_threads', 'wiki_sources', 'wiki_synthesis'
    ):
        os.makedirs(paths[key], exist_ok=True)
    return paths


def write_if_missing(path, content):
    """Write a file only when it does not already exist."""
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content.rstrip() + '\n')
    return True


def write_text_file(path, content):
    """Write text content, creating parent directories."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content.rstrip() + '\n')


def read_text_file(path):
    """Read text with replacement for malformed bytes."""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def append_memory_log(action, title, details=None):
    """Append an event to the LLM Wiki chronological log."""
    paths = ensure_memory_dirs()
    now = datetime.utcnow().isoformat()
    detail_text = f" | {details}" if details else ""
    with open(paths['log'], 'a', encoding='utf-8') as f:
        f.write(f"## [{now}] {action} | {title}{detail_text}\n\n")


def sha256_short(text):
    """Return a short SHA-256 checksum for text."""
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()[:16]


def unique_node_id(db, base_id):
    """Return a node id that does not already exist."""
    candidate = base_id
    while db.get_node(candidate):
        candidate = f"{base_id}-{random_hex(4)}"
    return candidate


def markdown_frontmatter(fields):
    """Serialize a small frontmatter dict without external YAML deps."""
    lines = ['---']
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, list):
            lines.append(f'{key}:')
            for item in value:
                lines.append(f'  - "{str(item).replace(chr(34), chr(39))}"')
        else:
            safe = str(value).replace('"', "'")
            lines.append(f'{key}: "{safe}"')
    lines.append('---')
    return '\n'.join(lines)


def extract_frontmatter_id(path):
    """Extract an id field from simple YAML frontmatter."""
    try:
        text = read_text_file(path)
    except OSError:
        return None
    if not text.startswith('---'):
        return None
    lines = text.splitlines()
    for line in lines[1:80]:
        if line.strip() == '---':
            break
        if line.startswith('id:'):
            return line.split(':', 1)[1].strip().strip('"')
    return None


def fetch_url_text(url, timeout=20):
    """Fetch URL text best-effort for raw-source capture."""
    request = Request(url, headers={'User-Agent': 'context-memory-capture/1.0'})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
        encoding = response.headers.get_content_charset() or 'utf-8'
    text = raw.decode(encoding, errors='replace')
    # Cheap HTML cleanup. This is intentionally simple; raw source remains raw.
    text = re.sub(r'(?is)<(script|style).*?>.*?</\1>', ' ', text)
    text = re.sub(r'(?s)<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def ensure_thread(db, thread_name, default_body=None):
    """Ensure a thread node exists and return its id."""
    thread_id = f"thread:{thread_name}"
    if not db.get_node(thread_id):
        db.add_node(
            id=thread_id,
            type='thread',
            name=thread_name,
            body=default_body or "(captured source; current position not synthesized yet)"
        )
    return thread_id


def is_question_resolved(db, question_id):
    """Return True if a question has an outgoing RESOLVED_BY edge."""
    edges = db.get_edges(question_id, direction='outgoing',
                         relationship_filter='RESOLVED_BY')
    return bool(edges)


def memory_schema_template():
    """Default schema.md content for the LLM Wiki layer."""
    return """# LLM Wiki Schema

This folder implements Andrej Karpathy's LLM Wiki pattern for this workspace.
The point is not more retrieval. The point is a maintained source of truth that
separates raw evidence from current beliefs and discarded ideas.

Source pattern: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

## Layers

- `raw/`: immutable source material. Agent reads this layer but does not rewrite
  captured files. This includes clipped tweets, blogs, docs, chat summaries, and
  external notes.
- `wiki/`: compiled markdown memory. Agents may update this layer. It contains
  current state, thread pages, source ledgers, open questions, discarded ideas,
  and higher-order synthesis.
- `schema.md`: this operating manual. Update it when the memory workflow changes.
- `context.db`: graph index and session log. It tracks sessions, concepts,
  sources, questions, shifts, confidence, and relationships.

## Status Model

Every important idea should be treated as one of:

- `active`: current working belief or direction.
- `uncertain`: plausible but not yet settled.
- `superseded`: replaced by newer thinking.
- `discarded`: should not be used as current context except historically.

Old ideas are not deleted. They are explicitly marked so future agents can see
why they changed without reviving them as active strategy.

## Required Workflows

### Ingest

Use `./context-safe.sh memory_capture` for any new tweet, article, blog,
meeting note, Claude/ChatGPT/Codex export, PDF notes, or important pasted text.
Then run `./context-safe.sh memory_compile` so the wiki reflects the graph.

### Query

Read `wiki/index.md` and `wiki/current-state.md` first. For a topic-specific
answer, read the relevant `wiki/threads/*.md` pages before searching raw files.
Raw sources are evidence; compiled wiki pages are the default current state.

### Lint

Run `./context-safe.sh memory_lint` periodically. Fix stale current-state pages,
missing source files, open questions that were resolved in conversation, and any
thread where discarded ideas are still being used as active claims.

## Agent Behavior

- Do not answer from old raw chats alone when a compiled thread page exists.
- Prefer current-state pages over historical source files.
- Cite raw files when making factual claims from external sources.
- If a conclusion changes, log a shift instead of overwriting history silently.
- If an answer produces a useful new synthesis, save it back into the wiki.
"""


def memory_index_template():
    """Default wiki/index.md content."""
    return """# LLM Wiki Index

This index is maintained by agents and `memory_compile`.

## Core Pages

- [[current-state]] - current active positions by thread.
- [[discarded-ideas]] - ideas that were superseded or should not be revived.
- [[open-questions]] - unresolved questions.
- [[source-ledger]] - captured raw sources.
- [[session-ledger]] - session timeline.

## Thread Pages

Run `./context-safe.sh memory_compile` to regenerate this section.
"""


def memory_log_template():
    """Default wiki/log.md content."""
    return """# LLM Wiki Log

Append-only chronological log of memory ingests, compiles, queries, and lint
passes. Entries begin with `## [` so agents can grep them quickly.
"""


# CLI Command Functions

def cmd_start_session(args, db):
    """Start a new session.

    Creates a session node and stores its ID in .context_session file.
    """
    title = args.title
    today = get_today()
    slug = slugify(title)
    session_id = f"session:{today}-{slug}"
    session_id = unique_node_id(db, session_id)

    db.add_node(
        id=session_id,
        type='session',
        name=title,
        body=f"Session started: {title}"
    )

    save_active_session(session_id)

    print(f"Session created: {session_id}")
    return session_id


def cmd_log_decision(args, db):
    """Log a decision in the active session.

    Creates a concept node and updates/creates threads with ADVANCED and RELATES_TO edges.
    """
    session_id = get_active_session()
    if not session_id:
        print("Error: No active session. Run 'start_session' first.", file=sys.stderr)
        sys.exit(1)

    description = args.description
    threads = [t.strip() for t in args.threads.split(',')]

    today = get_today()
    slug = slugify(description)
    hex_id = random_hex(4)
    concept_id = f"concept:{today}-{slug}-{hex_id}"

    # Create concept node
    db.add_node(
        id=concept_id,
        type='concept',
        name=f"Decision: {description[:50]}",
        body=description
    )
    db.add_edge(concept_id, session_id, 'LOGGED_IN', context=description)

    # Process each thread
    for thread_name in threads:
        thread_id = f"thread:{thread_name}"

        # Check if thread exists
        existing_thread = db.get_node(thread_id)

        if existing_thread:
            # Update existing thread's body
            db.update_node(thread_id, body=description)
        else:
            # Create new thread
            db.add_node(
                id=thread_id,
                type='thread',
                name=thread_name,
                body=description
            )

        # Create ADVANCED edge from session to thread
        db.add_edge(session_id, thread_id, 'ADVANCED', context=description)

        # Create RELATES_TO edge from concept to thread
        db.add_edge(concept_id, thread_id, 'RELATES_TO', context=description)

    print(f"Decision logged: {concept_id}")
    return concept_id


def cmd_log_question(args, db):
    """Log a question in the active session.

    Creates a question node with RAISED_IN and RELATES_TO edges.
    """
    session_id = get_active_session()
    if not session_id:
        print("Error: No active session. Run 'start_session' first.", file=sys.stderr)
        sys.exit(1)

    question = args.question
    threads = [t.strip() for t in args.threads.split(',')]

    today = get_today()
    slug = slugify(question)
    hex_id = random_hex(4)
    question_id = f"question:{today}-{slug}-{hex_id}"

    # Create question node
    db.add_node(
        id=question_id,
        type='question',
        name=question[:100],
        body=question
    )

    # Create RAISED_IN edge from question to session
    db.add_edge(question_id, session_id, 'RAISED_IN', context=question)

    # Create RELATES_TO edges to threads
    for thread_name in threads:
        thread_id = f"thread:{thread_name}"
        db.add_edge(question_id, thread_id, 'RELATES_TO', context=question)

    print(f"Question logged: {question_id}")
    return question_id


def cmd_log_shift(args, db):
    """Log a shift in thinking on a thread.

    Creates two concept nodes (old and new) with an EVOLVED_FROM edge between them.
    Updates the thread's body and creates ADVANCED edge from session to thread.
    """
    session_id = get_active_session()
    if not session_id:
        print("Error: No active session. Run 'start_session' first.", file=sys.stderr)
        sys.exit(1)

    description = args.description
    thread_name = args.thread
    old_position = args.from_position
    new_position = args.to_position

    today = get_today()
    slug = slugify(description)
    hex_id_old = random_hex(4)
    hex_id_new = random_hex(4)

    old_concept_id = f"concept:{today}-old-{slug}-{hex_id_old}"
    new_concept_id = f"concept:{today}-new-{slug}-{hex_id_new}"
    thread_id = f"thread:{thread_name}"

    # Create old position concept node
    db.add_node(
        id=old_concept_id,
        type='concept',
        name=f"Old: {old_position[:50]}",
        body=old_position
    )
    db.add_edge(old_concept_id, session_id, 'LOGGED_IN', context=description)

    # Create new position concept node
    db.add_node(
        id=new_concept_id,
        type='concept',
        name=f"New: {new_position[:50]}",
        body=new_position
    )
    db.add_edge(new_concept_id, session_id, 'LOGGED_IN', context=description)

    # Create EVOLVED_FROM edge from new to old
    db.add_edge(new_concept_id, old_concept_id, 'EVOLVED_FROM', context=description)

    # Ensure thread exists
    existing_thread = db.get_node(thread_id)
    if not existing_thread:
        db.add_node(
            id=thread_id,
            type='thread',
            name=thread_name,
            body=new_position
        )
    else:
        # Update thread's body to new position
        db.update_node(thread_id, body=new_position)

    # Create ADVANCED edge from session to thread
    db.add_edge(session_id, thread_id, 'ADVANCED', context=description)

    print(f"Shift logged: {new_concept_id} (evolved from {old_concept_id})")
    return (old_concept_id, new_concept_id)


def cmd_create_thread(args, db):
    """Create a new thread node.

    Creates a thread with optional RELATES_TO edges to other threads.
    """
    name = args.name
    current_position = args.current_position
    relates_to = args.relates_to

    thread_id = f"thread:{name}"

    # Create thread node
    db.add_node(
        id=thread_id,
        type='thread',
        name=name,
        body=current_position
    )

    # Create RELATES_TO edges if specified
    if relates_to:
        related_threads = [t.strip() for t in relates_to.split(',')]
        for related_name in related_threads:
            related_id = f"thread:{related_name}"
            db.add_edge(thread_id, related_id, 'RELATES_TO')

    print(f"Thread created: {thread_id}")
    return thread_id


def cmd_recent(args, db):
    """Display recent sessions with connected threads and what changed."""
    sessions = db.get_recent_sessions(args.sessions)

    if not sessions:
        print("No sessions found.")
        return

    # Compute confidence scores
    engine = ConfidenceEngine(db)
    scores = engine.compute_all()

    print(f"\n=== Recent Sessions (last {len(sessions)}) ===\n")

    # Collect all threads across sessions for confidence display
    all_threads = set()

    for session_entry in sessions:
        session = session_entry['session']
        threads = session_entry['threads']

        date_str = session['id'].split(':')[1][:10]

        print(f"[{date_str}] {session['name']}")
        if session['body']:
            context = session['body'][:80]
            if len(session['body']) > 80:
                context += "..."
            print(f"  Context: {context}")

        if threads:
            print("  Threads advanced:")
            # Sort threads by confidence
            thread_items = []
            for thread_info in threads:
                thread = thread_info['thread']
                tid = thread['id']
                conf = scores.get(tid, {}).get('confidence', 0.5)
                thread_items.append((conf, thread_info))
                all_threads.add(tid)

            thread_items.sort(key=lambda x: -x[0])

            for conf, thread_info in thread_items:
                thread = thread_info['thread']
                what_changed = thread_info.get('what_changed', '')

                # Skip low-confidence threads unless --all
                show_all = getattr(args, 'all', False)
                if conf < 0.3 and not show_all:
                    continue

                position = thread['body'][:60] if thread['body'] else "(no position)"
                if thread['body'] and len(thread['body']) > 60:
                    position = position + "..."

                change_text = what_changed[:50] if what_changed else "(no context)"
                if what_changed and len(what_changed) > 50:
                    change_text = change_text + "..."

                print(f"    - [{conf:.2f}] {thread['name']}: \"{position}\" | Changed: \"{change_text}\"")
        print()

    # Show thread confidence summary
    print("=== Thread Confidence ===\n")
    thread_scores = []
    for nid, data in scores.items():
        if data['inferred_type'] == 'thread_rollup':
            node = db.get_node(nid)
            name = node['name'] if node else nid
            thread_scores.append((data['confidence'], name))

    thread_scores.sort(key=lambda x: -x[0])
    show_all = getattr(args, 'all', False)
    for conf, name in thread_scores:
        if conf < 0.3 and not show_all:
            continue
        print(f"  [{conf:.2f}] {name}")
    print()


def cmd_get_context(args, db):
    """Get context around a search term by finding it and showing neighbors.

    Displays threads, concepts, questions, and sessions connected within 2 hops.
    Annotates each node with its confidence score.
    """
    search_term = args.search_term
    matches = db.find_nodes(search_term)

    if not matches:
        print(f"No nodes found matching '{search_term}'")
        return

    # Compute confidence scores
    engine = ConfidenceEngine(db)
    scores = engine.compute_all()

    print(f"\n=== Context for \"{search_term}\" ===\n")

    # Process each match
    for match in matches[:5]:  # Limit to first 5 matches
        match_conf = scores.get(match['id'], {}).get('confidence', 0)
        print(f"Starting from: [{match_conf:.2f}] {match['id']} ({match['type']})")
        if match['type'] == 'thread':
            print(f"  Current position: {match['body']}")
        elif match['body']:
            print(f"  Body: {match['body'][:60]}...")
        print()

        # Get neighbors within 2 hops
        neighbors = db.get_neighbors(match['id'], hops=2)

        # Group by type and relationship
        by_type = {}
        for neighbor in neighbors:
            node_type = neighbor['node']['type']
            if node_type not in by_type:
                by_type[node_type] = []
            by_type[node_type].append(neighbor)

        # Display connected threads (sorted by confidence)
        if 'thread' in by_type:
            print("  Connected threads (within 2 hops):")
            items = sorted(by_type['thread'],
                          key=lambda n: scores.get(n['node']['id'], {}).get('confidence', 0),
                          reverse=True)
            for neighbor in items:
                nid = neighbor['node']['id']
                depth = neighbor['depth']
                rel = neighbor['edge']['relationship']
                body = neighbor['node']['body'][:50] if neighbor['node']['body'] else ""
                conf = scores.get(nid, {}).get('confidence', 0)
                print(f"    - [{conf:.2f}] {neighbor['node']['name']}: \"{body}\" (via {rel}, depth {depth})")
            print()

        # Display related concepts (sorted by confidence)
        if 'concept' in by_type:
            print("  Related concepts:")
            items = sorted(by_type['concept'],
                          key=lambda n: scores.get(n['node']['id'], {}).get('confidence', 0),
                          reverse=True)
            for neighbor in items:
                nid = neighbor['node']['id']
                body = neighbor['node']['body'][:60]
                conf = scores.get(nid, {}).get('confidence', 0)
                print(f"    - [{conf:.2f}] {body}...")
            print()

        # Display open questions (sorted by confidence)
        if 'question' in by_type:
            print("  Open questions:")
            items = sorted(by_type['question'],
                          key=lambda n: scores.get(n['node']['id'], {}).get('confidence', 0),
                          reverse=True)
            for neighbor in items:
                nid = neighbor['node']['id']
                body = neighbor['node']['body']
                conf = scores.get(nid, {}).get('confidence', 0)
                print(f"    - [{conf:.2f}] {body}")
            print()

        # Display connected sessions
        if 'session' in by_type:
            print("  Recent sessions touching this:")
            for neighbor in by_type['session']:
                nid = neighbor['node']['id']
                date_str = nid.split(':')[1][:10]
                conf = scores.get(nid, {}).get('confidence', 0)
                print(f"    - [{conf:.2f}] [{date_str}] {neighbor['node']['name']} (via {neighbor['edge']['relationship']})")
            print()


def cmd_get_thread(args, db):
    """Get the current position and history of a thread.

    Shows chronological history of sessions that ADVANCED this thread.
    """
    thread_name = args.thread_name
    thread_id = f"thread:{thread_name}"

    # Try exact match first, then fuzzy match
    thread_node = db.get_node(thread_id)
    if not thread_node:
        matches = db.find_nodes(thread_name, type_filter='thread')
        if not matches:
            print(f"Thread not found: {thread_name}")
            return
        thread_node = matches[0]
        thread_id = thread_node['id']

    print(f"\n=== Thread: {thread_node['name']} ===\n")
    print(f"Current Position: {thread_node['body']}\n")

    # Get all sessions that ADVANCED this thread
    cursor = db.conn.cursor()
    cursor.execute('''
        SELECT edges.*, nodes.id as session_id, nodes.name, nodes.created_at
        FROM edges
        JOIN nodes ON edges.from_node = nodes.id
        WHERE edges.to_node = ? AND edges.relationship = 'ADVANCED'
        ORDER BY nodes.created_at ASC
    ''', (thread_id,))

    rows = cursor.fetchall()

    if rows:
        print("Advancement History (chronological):")
        for row in rows:
            session_id = row['session_id']
            date_str = session_id.split(':')[1][:10] if ':' in session_id else "unknown"
            session_name = row['name']
            context = row['context'] if row['context'] else "(no context)"
            print(f"  [{date_str}] {session_name}: {context}")
        print()
    else:
        print("(No session history yet)\n")

    # Get related threads
    edges = db.get_edges(thread_id, direction='both', relationship_filter='RELATES_TO')
    if edges:
        print("Related Threads:")
        for edge in edges:
            related_id = edge['to_node'] if edge['from_node'] == thread_id else edge['from_node']
            related_node = db.get_node(related_id)
            if related_node:
                print(f"  - {related_node['name']}")
        print()

    # Get open questions related to this thread
    neighbors = db.get_neighbors(thread_id, hops=1, relationship_filter='RELATES_TO')
    questions = [n for n in neighbors if n['node']['type'] == 'question']
    if questions:
        print("Related Open Questions:")
        for neighbor in questions:
            print(f"  - {neighbor['node']['body']}")


def cmd_open_questions(args, db):
    """List all open questions (those without RESOLVED_BY edges).

    Shows question text, when raised, and related threads.
    """
    cursor = db.conn.cursor()

    # Get all question nodes
    cursor.execute('SELECT * FROM nodes WHERE type = ?', ('question',))
    all_questions = [dict(row) for row in cursor.fetchall()]

    # Filter to open questions (no RESOLVED_BY edge)
    open_qs = []
    for q in all_questions:
        cursor.execute(
            'SELECT COUNT(*) as cnt FROM edges WHERE from_node = ? AND relationship = ?',
            (q['id'], 'RESOLVED_BY')
        )
        resolved = cursor.fetchone()['cnt']
        if resolved == 0:
            open_qs.append(q)

    if not open_qs:
        print("No open questions.")
        return

    print(f"\n=== Open Questions ({len(open_qs)} total) ===\n")

    for q in sorted(open_qs, key=lambda x: x['created_at'], reverse=True):
        date_str = q['created_at'][:10] if q['created_at'] else "unknown"
        print(f"[{date_str}] {q['body']}")

        # Find related threads via RELATES_TO
        edges = db.get_edges(q['id'], direction='outgoing', relationship_filter='RELATES_TO')
        if edges:
            threads = [db.get_node(edge['to_node']) for edge in edges]
            threads = [t for t in threads if t and t['type'] == 'thread']
            if threads:
                thread_names = ", ".join([t['name'] for t in threads])
                print(f"  Relates to: {thread_names}")
        print()


def cmd_relate(args, db):
    """Create a RELATES_TO edge between two nodes.

    Validates both nodes exist before creating the edge.
    """
    node1_id = args.node1
    node2_id = args.node2

    # Validate both nodes exist
    node1 = db.get_node(node1_id)
    if not node1:
        print(f"Error: Node not found: {node1_id}", file=sys.stderr)
        sys.exit(1)

    node2 = db.get_node(node2_id)
    if not node2:
        print(f"Error: Node not found: {node2_id}", file=sys.stderr)
        sys.exit(1)

    # Create RELATES_TO edge
    db.add_edge(node1_id, node2_id, 'RELATES_TO')

    print(f"Relation created: {node1_id} -> {node2_id}")


def cmd_resolve_question(args, db):
    """Create a RESOLVED_BY edge from a question to a session.

    Validates both nodes exist before creating the edge.
    """
    question_id = args.question_id
    session_id = args.session

    # Validate question exists
    question = db.get_node(question_id)
    if not question:
        print(f"Error: Question not found: {question_id}", file=sys.stderr)
        sys.exit(1)

    # Validate session exists
    session = db.get_node(session_id)
    if not session:
        print(f"Error: Session not found: {session_id}", file=sys.stderr)
        sys.exit(1)

    # Create RESOLVED_BY edge from question to session
    db.add_edge(question_id, session_id, 'RESOLVED_BY')

    print(f"Resolution created: {question_id} resolved by {session_id}")


def cmd_import_source(args, db):
    """Import a file as a source node and optionally relate it to other nodes.

    Reads file content (truncates if over 5000 chars to first 500 chars).
    Creates a source node with id source:<slugified-name>.
    Creates RELATES_TO edges to specified nodes if --relates-to provided.
    """
    file_path = args.file_path
    name = args.name
    relates_to = args.relates_to

    # Check file exists
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    # Read file content
    with open(file_path, 'r') as f:
        content = f.read()

    # Truncate if over 5000 chars
    if len(content) > 5000:
        body = content[:500]
    else:
        body = content

    # Create source node ID
    slug = slugify(name)
    source_id = f"source:{slug}"

    # Store file path in metadata
    metadata = {"file_path": file_path}

    # Create source node
    db.add_node(
        id=source_id,
        type='source',
        name=name,
        body=body,
        metadata=metadata
    )

    # Create RELATES_TO edges if specified
    if relates_to:
        related_nodes = [n.strip() for n in relates_to.split(',')]
        for related_id in related_nodes:
            # Validate related node exists
            related_node = db.get_node(related_id)
            if not related_node:
                print(f"Warning: Related node not found: {related_id}", file=sys.stderr)
                continue

            db.add_edge(source_id, related_id, 'RELATES_TO')

    print(f"Source imported: {source_id}")
    return source_id


def cmd_memory_init(args, db):
    """Initialize the Karpathy-style LLM Wiki directory structure."""
    paths = ensure_memory_dirs()

    write_if_missing(paths['schema'], memory_schema_template())
    write_if_missing(paths['index'], memory_index_template())
    write_if_missing(paths['log'], memory_log_template())
    write_if_missing(paths['current_state'], "# Current State\n\nRun `./context-safe.sh memory_compile` to generate this page.\n")
    write_if_missing(paths['discarded'], "# Discarded Ideas\n\nRun `./context-safe.sh memory_compile` to generate this page.\n")
    write_if_missing(paths['open_questions'], "# Open Questions\n\nRun `./context-safe.sh memory_compile` to generate this page.\n")
    write_if_missing(paths['source_ledger'], "# Source Ledger\n\nRun `./context-safe.sh memory_compile` to generate this page.\n")
    write_if_missing(paths['session_ledger'], "# Session Ledger\n\nRun `./context-safe.sh memory_compile` to generate this page.\n")
    append_memory_log('init', 'LLM Wiki initialized')

    print(f"LLM Wiki initialized: {paths['root']}")
    return paths['root']


def cmd_memory_capture(args, db):
    """Capture a raw source into the LLM Wiki and graph."""
    paths = ensure_memory_dirs()

    title = args.title.strip()
    kind = args.kind.strip().lower()
    url = args.url.strip() if args.url else None
    notes = args.notes.strip() if args.notes else ''
    tags = [t.strip() for t in (args.tags or '').split(',') if t.strip()]
    threads = [t.strip() for t in (args.threads or '').split(',') if t.strip()]

    pieces = []
    original_file = None

    if args.file:
        original_file = os.path.abspath(args.file)
        if not os.path.exists(original_file):
            print(f"Error: File not found: {original_file}", file=sys.stderr)
            sys.exit(1)
        pieces.append(read_text_file(original_file))

    if args.text:
        pieces.append(args.text)

    if args.stdin:
        pieces.append(sys.stdin.read())

    if url and args.fetch:
        try:
            pieces.append(fetch_url_text(url))
        except (OSError, URLError, TimeoutError) as exc:
            print(f"Warning: URL fetch failed, storing URL and notes only: {exc}", file=sys.stderr)

    source_text = '\n\n'.join(p.strip() for p in pieces if p and p.strip())
    if not source_text:
        if notes:
            source_text = notes
        elif url:
            source_text = f"Captured URL: {url}"
        else:
            source_text = "(no source text captured)"

    today = get_today()
    slug = slugify(title) or 'untitled-source'
    hex_id = random_hex(6)
    source_id = unique_node_id(db, f"source:{today}-{slug}-{hex_id}")
    checksum = sha256_short(source_text)

    raw_dir = os.path.join(paths['raw_sources'], kind)
    os.makedirs(raw_dir, exist_ok=True)
    raw_filename = f"{today}-{slug}-{hex_id}.md"
    raw_path = os.path.join(raw_dir, raw_filename)

    frontmatter = markdown_frontmatter({
        'id': source_id,
        'type': 'source',
        'kind': kind,
        'title': title,
        'url': url,
        'captured_at': datetime.utcnow().isoformat(),
        'status': 'raw',
        'checksum': checksum,
        'threads': threads,
        'tags': tags,
        'original_file': original_file,
    })

    raw_body = [
        frontmatter,
        '',
        f"# {title}",
        '',
        "## Capture Notes",
        notes or "(none)",
        '',
        "## Source",
    ]
    if url:
        raw_body.append(f"- URL: {url}")
    if original_file:
        raw_body.append(f"- Original file: {original_file}")
    raw_body.extend([
        f"- Checksum: `{checksum}`",
        '',
        "## Source Text",
        source_text,
    ])
    write_text_file(raw_path, '\n'.join(raw_body))

    metadata = {
        'file_path': raw_path,
        'url': url,
        'kind': kind,
        'tags': tags,
        'status': 'raw',
        'checksum': checksum,
        'captured_at': datetime.utcnow().isoformat(),
        'original_file': original_file,
    }
    graph_body = (notes + '\n\n' + source_text).strip()
    db.add_node(
        id=source_id,
        type='source',
        name=title,
        body=graph_body[:2000],
        metadata=metadata
    )

    for thread_name in threads:
        thread_id = ensure_thread(db, thread_name)
        db.add_edge(source_id, thread_id, 'RELATES_TO', context=f"Captured source: {title}")

    append_memory_log('ingest', title, f"id={source_id}; path={raw_path}")
    print(f"Memory source captured: {source_id}")
    print(f"Raw file: {raw_path}")
    return source_id


def _node_body(node, limit=None):
    body = node.get('body') or ''
    if limit and len(body) > limit:
        return body[:limit].rstrip() + '...'
    return body


def _thread_history(db, thread_id, limit=8):
    cursor = db.conn.cursor()
    cursor.execute('''
        SELECT edges.context, nodes.id as session_id, nodes.name, nodes.created_at
        FROM edges
        JOIN nodes ON edges.from_node = nodes.id
        WHERE edges.to_node = ? AND edges.relationship = 'ADVANCED'
        ORDER BY nodes.created_at DESC
        LIMIT ?
    ''', (thread_id, limit))
    return [dict(row) for row in cursor.fetchall()]


def _all_nodes_of_type(db, node_type):
    cursor = db.conn.cursor()
    cursor.execute('SELECT * FROM nodes WHERE type = ? ORDER BY updated_at DESC', (node_type,))
    rows = []
    for row in cursor.fetchall():
        node = dict(row)
        if node['metadata']:
            try:
                node['metadata'] = json.loads(node['metadata'])
            except json.JSONDecodeError:
                node['metadata'] = {}
        rows.append(node)
    return rows


def _related_nodes(db, node_id, relationship='RELATES_TO'):
    edges = db.get_edges(node_id, direction='both', relationship_filter=relationship)
    related = []
    seen = set()
    for edge in edges:
        other_id = edge['to_node'] if edge['from_node'] == node_id else edge['from_node']
        if other_id in seen:
            continue
        seen.add(other_id)
        node = db.get_node(other_id)
        if node:
            related.append((node, edge))
    return related


def cmd_memory_compile(args, db):
    """Compile graph memory into the LLM Wiki current-state pages."""
    paths = ensure_memory_dirs()
    write_if_missing(paths['schema'], memory_schema_template())
    write_if_missing(paths['log'], memory_log_template())

    engine = ConfidenceEngine(db)
    scores = engine.compute_all()
    now = datetime.utcnow().isoformat()

    threads = _all_nodes_of_type(db, 'thread')
    threads.sort(key=lambda n: scores.get(n['id'], {}).get('confidence', 0), reverse=True)

    thread_links = []
    discarded_items = []
    open_question_items = []

    for thread in threads:
        thread_id = thread['id']
        thread_name = thread['name']
        confidence = scores.get(thread_id, {}).get('confidence', 0.5)
        slug = slugify(thread_name) or 'thread'
        thread_path = os.path.join(paths['wiki_threads'], f"{slug}.md")

        concepts = []
        sources = []
        questions = []
        related_threads = []

        for node, edge in _related_nodes(db, thread_id):
            if node['type'] == 'concept':
                inferred = scores.get(node['id'], {}).get('inferred_type', 'active_direction')
                concepts.append((node, inferred))
                if inferred == 'superseded':
                    discarded_items.append((thread_name, node))
            elif node['type'] == 'source':
                sources.append(node)
            elif node['type'] == 'question':
                if not is_question_resolved(db, node['id']):
                    questions.append(node)
                    open_question_items.append((thread_name, node))
            elif node['type'] == 'thread':
                related_threads.append(node)

        history = _thread_history(db, thread_id)
        active_concepts = [item for item in concepts if item[1] != 'superseded']
        superseded_concepts = [item for item in concepts if item[1] == 'superseded']

        lines = [
            markdown_frontmatter({
                'type': 'thread',
                'thread': thread_name,
                'generated_at': now,
                'confidence': f"{confidence:.2f}",
                'status': 'active',
            }),
            '',
            f"# {thread_name}",
            '',
            f"Confidence: `{confidence:.2f}`",
            '',
            "## Current Position",
            _node_body(thread) or "(no current position recorded)",
            '',
            "## Recent Changes",
        ]
        if history:
            for item in history:
                date_str = (item['session_id'].split(':', 1)[1][:10]
                            if ':' in item['session_id'] else 'unknown')
                context = item['context'] or '(no context)'
                lines.append(f"- {date_str}: {context}")
        else:
            lines.append("- (no session history)")

        lines.extend(['', "## Active Decisions"])
        if active_concepts:
            for concept, inferred in active_concepts[:20]:
                lines.append(f"- [{inferred}] {_node_body(concept, 280)}")
        else:
            lines.append("- (none recorded)")

        lines.extend(['', "## Superseded Or Discarded"])
        if superseded_concepts:
            for concept, inferred in superseded_concepts[:20]:
                lines.append(f"- {_node_body(concept, 280)}")
        else:
            lines.append("- (none recorded)")

        lines.extend(['', "## Open Questions"])
        if questions:
            for question in questions:
                lines.append(f"- {_node_body(question, 260)}")
        else:
            lines.append("- (none recorded)")

        lines.extend(['', "## Sources"])
        if sources:
            for source in sources[:30]:
                meta = source.get('metadata') or {}
                path = meta.get('file_path', '')
                url = meta.get('url', '')
                suffix = f" ({url})" if url else ""
                lines.append(f"- {source['name']}{suffix} - `{path}`")
        else:
            lines.append("- (none linked)")

        lines.extend(['', "## Related Threads"])
        if related_threads:
            for related in related_threads:
                lines.append(f"- [[{slugify(related['name'])}|{related['name']}]]")
        else:
            lines.append("- (none linked)")

        write_text_file(thread_path, '\n'.join(lines))
        thread_links.append((thread_name, slug, confidence, _node_body(thread, 180)))

    current_lines = [
        markdown_frontmatter({'type': 'current-state', 'generated_at': now}),
        '',
        "# Current State",
        '',
        "This page is generated from `context.db`. It is the default starting point for agents.",
        '',
        "## Active Threads",
    ]
    for thread_name, slug, confidence, body in thread_links:
        current_lines.append(f"- [{confidence:.2f}] [[threads/{slug}|{thread_name}]] - {body}")
    write_text_file(paths['current_state'], '\n'.join(current_lines))

    discarded_lines = [
        markdown_frontmatter({'type': 'discarded-ideas', 'generated_at': now}),
        '',
        "# Discarded Ideas",
        '',
        "These ideas were superseded or outcompeted. Do not use them as current context unless explicitly reviewing history.",
    ]
    if discarded_items:
        for thread_name, concept in discarded_items:
            discarded_lines.append(f"- {thread_name}: {_node_body(concept, 320)}")
    else:
        discarded_lines.append("- (none recorded)")
    write_text_file(paths['discarded'], '\n'.join(discarded_lines))

    question_lines = [
        markdown_frontmatter({'type': 'open-questions', 'generated_at': now}),
        '',
        "# Open Questions",
        '',
    ]
    if open_question_items:
        seen = set()
        for thread_name, question in open_question_items:
            if question['id'] in seen:
                continue
            seen.add(question['id'])
            question_lines.append(f"- {thread_name}: {_node_body(question, 320)}")
    else:
        question_lines.append("- (none recorded)")
    write_text_file(paths['open_questions'], '\n'.join(question_lines))

    sources = _all_nodes_of_type(db, 'source')
    source_lines = [
        markdown_frontmatter({'type': 'source-ledger', 'generated_at': now}),
        '',
        "# Source Ledger",
        '',
    ]
    if sources:
        for source in sources:
            meta = source.get('metadata') or {}
            path = meta.get('file_path', '')
            url = meta.get('url', '')
            source_lines.append(f"- {source['name']} - `{source['id']}`")
            if url:
                source_lines.append(f"  - URL: {url}")
            if path:
                source_lines.append(f"  - File: `{path}`")
    else:
        source_lines.append("- (none recorded)")
    write_text_file(paths['source_ledger'], '\n'.join(source_lines))

    sessions = _all_nodes_of_type(db, 'session')[:50]
    session_lines = [
        markdown_frontmatter({'type': 'session-ledger', 'generated_at': now}),
        '',
        "# Session Ledger",
        '',
    ]
    for session in sessions:
        date_str = session['id'].split(':', 1)[1][:10] if ':' in session['id'] else 'unknown'
        session_lines.append(f"- {date_str}: {session['name']} - {_node_body(session, 180)}")
    write_text_file(paths['session_ledger'], '\n'.join(session_lines))

    index_lines = [
        "# LLM Wiki Index",
        '',
        f"Generated: `{now}`",
        '',
        "## Core Pages",
        '',
        "- [[current-state]] - current active positions by thread.",
        "- [[discarded-ideas]] - ideas that were superseded or should not be revived.",
        "- [[open-questions]] - unresolved questions.",
        "- [[source-ledger]] - captured raw sources.",
        "- [[session-ledger]] - session timeline.",
        '',
        "## Thread Pages",
        '',
    ]
    for thread_name, slug, confidence, body in thread_links:
        index_lines.append(f"- [[threads/{slug}|{thread_name}]] - confidence {confidence:.2f}")
    write_text_file(paths['index'], '\n'.join(index_lines))

    append_memory_log('compile', 'Graph compiled into LLM Wiki',
                      f"threads={len(threads)}; sources={len(sources)}")
    print(f"LLM Wiki compiled: {paths['wiki']}")
    print(f"Threads: {len(threads)}")
    print(f"Sources: {len(sources)}")


def cmd_memory_search(args, db):
    """Search the LLM Wiki raw and compiled markdown files."""
    paths = get_memory_paths()
    roots = []
    if args.raw or not args.wiki:
        roots.append(paths['raw'])
    if args.wiki or not args.raw:
        roots.append(paths['wiki'])

    term = args.term.lower()
    matches = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith('.md'):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    lines = read_text_file(path).splitlines()
                except OSError:
                    continue
                for idx, line in enumerate(lines, start=1):
                    if term in line.lower():
                        matches.append((path, idx, line.strip()))
                        break
                if len(matches) >= args.limit:
                    break
            if len(matches) >= args.limit:
                break
        if len(matches) >= args.limit:
            break

    if not matches:
        print(f"No LLM Wiki matches for '{args.term}'")
        return

    for path, line_no, line in matches:
        print(f"{path}:{line_no}: {line[:240]}")


def cmd_memory_lint(args, db):
    """Health-check the graph and LLM Wiki layer."""
    paths = get_memory_paths()
    issues = []
    warnings = []

    try:
        rows = db.conn.execute('PRAGMA integrity_check').fetchall()
        integrity_messages = [row[0] for row in rows]
        if integrity_messages != ['ok']:
            issues.append('context.db integrity_check failed: ' + '; '.join(integrity_messages))
    except sqlite3.DatabaseError as exc:
        issues.append(f'context.db integrity_check raised: {exc}')

    for key in ('root', 'raw', 'wiki', 'schema', 'index', 'current_state', 'discarded'):
        path = paths[key]
        if key in ('root', 'raw', 'wiki'):
            if not os.path.isdir(path):
                issues.append(f'Missing directory: {path}')
        elif not os.path.exists(path):
            issues.append(f'Missing file: {path}')

    for source in _all_nodes_of_type(db, 'source'):
        meta = source.get('metadata') or {}
        file_path = meta.get('file_path')
        if file_path and not os.path.exists(file_path):
            issues.append(f"Source node file missing: {source['id']} -> {file_path}")
        if not file_path:
            warnings.append(f"Source node has no file_path metadata: {source['id']}")

    raw_ids = []
    if os.path.isdir(paths['raw']):
        for dirpath, _, filenames in os.walk(paths['raw']):
            for filename in filenames:
                if filename.endswith('.md'):
                    raw_path = os.path.join(dirpath, filename)
                    raw_id = extract_frontmatter_id(raw_path)
                    if raw_id:
                        raw_ids.append(raw_id)
                        if not db.get_node(raw_id):
                            issues.append(f"Raw file not indexed in graph: {raw_path}")

    thread_count = len(_all_nodes_of_type(db, 'thread'))
    thread_page_count = 0
    if os.path.isdir(paths['wiki_threads']):
        thread_page_count = len([f for f in os.listdir(paths['wiki_threads']) if f.endswith('.md')])
    if thread_count and thread_page_count < thread_count:
        warnings.append(f"Thread pages may be stale: {thread_page_count} pages for {thread_count} graph threads")

    open_questions = [q for q in _all_nodes_of_type(db, 'question') if not is_question_resolved(db, q['id'])]
    if open_questions:
        warnings.append(f"{len(open_questions)} open questions remain")

    print("\n=== Memory Lint ===\n")
    if not issues and not warnings:
        print("ok")
    if issues:
        print("Issues:")
        for issue in issues:
            print(f"  - {issue}")
        print()
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
        print()

    print(f"Raw files indexed: {len(raw_ids)}")
    print(f"Threads in graph: {thread_count}")
    print(f"Thread pages: {thread_page_count}")

    if args.strict and issues:
        sys.exit(1)


def cmd_confidence(args, db):
    """Display confidence scores for all nodes or a specific thread.

    If thread_name is given, shows detailed breakdown.
    Otherwise shows all nodes ranked by confidence.
    """
    engine = ConfidenceEngine(db)
    scores = engine.compute_all()

    if args.thread_name:
        # Detailed view for one thread
        thread_id = f"thread:{args.thread_name}"
        thread_score = scores.get(thread_id)

        if not thread_score:
            print(f"Thread not found: {args.thread_name}")
            return

        thread_node = db.get_node(thread_id)
        print(f"\n=== Confidence: {args.thread_name} ===\n")
        print(f"Score: {thread_score['confidence']:.2f}")
        print(f"Inferred type: {thread_score['inferred_type']}")
        print(f"Current position: {thread_node['body'][:100] if thread_node else '(unknown)'}...")
        print()

        if thread_score['frequency_component'] is not None:
            print(f"Frequency component:   {thread_score['frequency_component']:.3f} (weight: {ConfidenceEngine.W_FREQUENCY})")
        if thread_score['support_component'] is not None:
            print(f"Support component:     {thread_score['support_component']:.3f} (weight: {ConfidenceEngine.W_SUPPORT})")
        if thread_score['competition_component'] is not None:
            comp = thread_score['competition_component']
            print(f"Competition penalty:   {comp:.3f} (weight: {ConfidenceEngine.W_COMPETITION})")
            if comp == 0:
                print(f"  -> Uncontested (floor applied)")
        print()

        # Show connected threads and their scores
        neighbors = db.get_neighbors(thread_id, hops=1,
                                      relationship_filter='RELATES_TO')
        if neighbors:
            print("Connected threads:")
            for n in neighbors:
                nid = n['node']['id']
                nscore = scores.get(nid, {}).get('confidence', 0)
                print(f"  [{nscore:.2f}] {n['node']['name']}")

    else:
        # All nodes ranked by confidence
        print(f"\n=== Confidence Scores ({len(scores)} nodes) ===\n")

        # Group by type
        by_type = defaultdict(list)
        for nid, data in scores.items():
            by_type[data['inferred_type']].append((nid, data))

        # Show threads first
        if 'thread_rollup' in by_type:
            print("Threads:")
            items = sorted(by_type['thread_rollup'], key=lambda x: -x[1]['confidence'])
            for nid, data in items:
                node = db.get_node(nid)
                name = node['name'] if node else nid
                print(f"  [{data['confidence']:.2f}] {name}")
            print()

        # Show active directions
        if 'active_direction' in by_type:
            print("Active Directions:")
            items = sorted(by_type['active_direction'], key=lambda x: -x[1]['confidence'])
            for nid, data in items[:10]:
                node = db.get_node(nid)
                body = node['body'][:60] if node and node['body'] else nid
                print(f"  [{data['confidence']:.2f}] {body}...")
            if len(by_type['active_direction']) > 10:
                print(f"  ... and {len(by_type['active_direction']) - 10} more")
            print()

        # Summary
        for t in ['settled_fact', 'superseded', 'open_question', 'stable_source', 'session']:
            if t in by_type:
                print(f"{t}: {len(by_type[t])} nodes")


def cmd_finalize_session(args, db):
    """Finalize the active session by writing a markdown file and cleaning up state.

    Reads the session node, queries connected edges, and writes a structured markdown
    file to Vault/09-Claude-Chats/YYYY-MM-DD-<slug>.md
    """
    session_id = get_active_session()
    if not session_id:
        print("Error: No active session. Run 'start_session' first.", file=sys.stderr)
        sys.exit(1)

    key_context = args.key_context

    # Get the session node
    session_node = db.get_node(session_id)
    if not session_node:
        print(f"Error: Session not found: {session_id}", file=sys.stderr)
        sys.exit(1)

    # Update session node with key context
    db.update_node(session_id, body=key_context)

    # Extract date and slug from session ID (format: session:YYYY-MM-DD-slug)
    id_parts = session_id.split(':', 1)[1]  # Remove 'session:' prefix
    date_str = id_parts[:10]  # YYYY-MM-DD
    slug = id_parts[11:]  # Everything after date-

    # Get all ADVANCED edges (sessions advancing threads)
    cursor = db.conn.cursor()
    cursor.execute('''
        SELECT * FROM edges
        WHERE from_node = ? AND relationship = 'ADVANCED'
    ''', (session_id,))
    advanced_edges = [dict(row) for row in cursor.fetchall()]

    # Get thread names from advanced edges
    thread_names = []
    for edge in advanced_edges:
        thread_node = db.get_node(edge['to_node'])
        if thread_node:
            thread_names.append(thread_node['name'])

    # Get decisions logged directly in this session. Older graph entries do not
    # have LOGGED_IN edges, so fall back to the historical thread query if needed.
    decisions = []
    cursor.execute('''
        SELECT * FROM edges
        WHERE to_node = ? AND relationship = 'LOGGED_IN'
    ''', (session_id,))
    logged_edges = cursor.fetchall()
    for logged_edge in logged_edges:
        concept_node = db.get_node(logged_edge['from_node'])
        if concept_node and concept_node['type'] == 'concept':
            decisions.append(concept_node['body'])

    if not decisions:
        for edge in advanced_edges:
            thread_id = edge['to_node']
            cursor.execute('''
                SELECT * FROM edges
                WHERE to_node = ? AND relationship = 'RELATES_TO'
            ''', (thread_id,))
            related_edges = cursor.fetchall()
            for rel_edge in related_edges:
                concept_node = db.get_node(rel_edge['from_node'])
                if concept_node and concept_node['type'] == 'concept':
                    decisions.append(concept_node['body'])

    # Get open questions (RAISED_IN edges pointing to this session)
    cursor.execute('''
        SELECT * FROM edges
        WHERE to_node = ? AND relationship = 'RAISED_IN'
    ''', (session_id,))
    question_edges = cursor.fetchall()
    questions = []
    for edge in question_edges:
        q_node = db.get_node(edge['from_node'])
        if q_node:
            questions.append(q_node['body'])

    # Build markdown content
    markdown_lines = []
    markdown_lines.append(f"# Chat: {session_node['name']}")
    markdown_lines.append(f"**Date:** {date_str}")
    if thread_names:
        thread_tags = ' '.join([f"#{name.lower().replace(' ', '-')}" for name in thread_names])
        markdown_lines.append(f"**Tags:** #claude-chat {thread_tags}")
    else:
        markdown_lines.append("**Tags:** #claude-chat")

    markdown_lines.append("")
    markdown_lines.append("## What Changed This Session")
    if advanced_edges:
        for edge in advanced_edges:
            thread_node = db.get_node(edge['to_node'])
            context_text = edge['context'] if edge['context'] else "(no context)"
            if thread_node:
                markdown_lines.append(f"- {thread_node['name']}: {context_text}")
    else:
        markdown_lines.append("(No threads advanced)")

    markdown_lines.append("")
    markdown_lines.append("## Decisions Made")
    if decisions:
        for decision in decisions:
            markdown_lines.append(f"- {decision}")
    else:
        markdown_lines.append("(No decisions logged)")

    markdown_lines.append("")
    markdown_lines.append("## Open Questions")
    if questions:
        for question in questions:
            markdown_lines.append(f"- {question}")
    else:
        markdown_lines.append("(No questions raised)")

    markdown_lines.append("")
    markdown_lines.append("## Key Context")
    markdown_lines.append(key_context)

    markdown_lines.append("")
    markdown_lines.append("## Related Threads")
    if thread_names:
        for thread_name in thread_names:
            markdown_lines.append(f"[[{thread_name}]]")
    else:
        markdown_lines.append("(No threads)")

    # Determine vault path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    vault_base = os.path.join(script_dir, 'Startup', 'Daily notes', 'Vault', '09-Claude-Chats')
    os.makedirs(vault_base, exist_ok=True)

    # Build filename: YYYY-MM-DD-<slug>.md
    filename = f"{date_str}-{slug}.md"
    filepath = os.path.join(vault_base, filename)

    # Write markdown file
    with open(filepath, 'w') as f:
        f.write('\n'.join(markdown_lines))
        f.write('\n')

    # Also capture every finalized session as an immutable raw chat source in
    # the LLM Wiki layer, so chat memory is not only a graph summary.
    memory_paths = ensure_memory_dirs()
    raw_chat_id = unique_node_id(db, f"source:chat-{date_str}-{slug}")
    raw_chat_path = os.path.join(memory_paths['raw_chats'], f"{date_str}-{slug}.md")
    raw_chat_content = '\n'.join([
        markdown_frontmatter({
            'id': raw_chat_id,
            'type': 'source',
            'kind': 'chat',
            'title': session_node['name'],
            'captured_at': datetime.utcnow().isoformat(),
            'status': 'raw',
            'session_id': session_id,
            'threads': thread_names,
        }),
        '',
        '\n'.join(markdown_lines),
    ])
    write_text_file(raw_chat_path, raw_chat_content)

    db.add_node(
        id=raw_chat_id,
        type='source',
        name=f"Chat: {session_node['name']}",
        body=key_context,
        metadata={
            'file_path': raw_chat_path,
            'kind': 'chat',
            'status': 'raw',
            'session_id': session_id,
            'captured_at': datetime.utcnow().isoformat(),
        }
    )
    db.add_edge(raw_chat_id, session_id, 'RELATES_TO', context='Finalized chat raw source')
    for thread_id in sorted({edge['to_node'] for edge in advanced_edges}):
        db.add_edge(raw_chat_id, thread_id, 'RELATES_TO', context='Finalized chat raw source')
    append_memory_log('chat', session_node['name'], f"id={raw_chat_id}; path={raw_chat_path}")

    print(f"Session finalized: {filepath}")

    # Remove active session state file
    state_file = get_state_file_path()
    if os.path.exists(state_file):
        os.remove(state_file)

    return filepath


if __name__ == '__main__':
    # Initialize database with default path
    db = Database()

    # Set up argument parser
    parser = argparse.ArgumentParser(
        description='Context database CLI for managing sessions, decisions, and threads'
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # start_session command
    start_parser = subparsers.add_parser(
        'start_session',
        help='Start a new session'
    )
    start_parser.add_argument('--title', required=True, help='Session title')

    # log_decision command
    decision_parser = subparsers.add_parser(
        'log_decision',
        help='Log a decision in the active session'
    )
    decision_parser.add_argument('description', help='Decision description')
    decision_parser.add_argument('--threads', required=True, help='Comma-separated thread names')

    # log_question command
    question_parser = subparsers.add_parser(
        'log_question',
        help='Log a question in the active session'
    )
    question_parser.add_argument('question', help='Question text')
    question_parser.add_argument('--threads', required=True, help='Comma-separated thread names')

    # log_shift command
    shift_parser = subparsers.add_parser(
        'log_shift',
        help='Log a shift in thinking on a thread'
    )
    shift_parser.add_argument('description', help='Description of the shift')
    shift_parser.add_argument('--thread', required=True, help='Thread name')
    shift_parser.add_argument('--from', dest='from_position', required=True, help='Old position')
    shift_parser.add_argument('--to', dest='to_position', required=True, help='New position')

    # create_thread command
    thread_parser = subparsers.add_parser(
        'create_thread',
        help='Create a new thread'
    )
    thread_parser.add_argument('name', help='Thread name')
    thread_parser.add_argument('--current-position', required=True, help='Initial position/body')
    thread_parser.add_argument('--relates-to', help='Comma-separated related thread names')

    # recent command
    recent_parser = subparsers.add_parser(
        'recent',
        help='Display recent sessions'
    )
    recent_parser.add_argument('--sessions', type=int, default=14, help='Number of sessions to show (default 14)')
    recent_parser.add_argument('--all', action='store_true',
                               help='Show all threads including low-confidence ones')

    # get_context command
    context_parser = subparsers.add_parser(
        'get_context',
        help='Get context around a search term'
    )
    context_parser.add_argument('search_term', help='Search term')

    # get_thread command
    thread_info_parser = subparsers.add_parser(
        'get_thread',
        help='Get current position and history of a thread'
    )
    thread_info_parser.add_argument('thread_name', help='Thread name')

    # open_questions command
    questions_parser = subparsers.add_parser(
        'open_questions',
        help='List all open questions'
    )

    # finalize_session command
    finalize_parser = subparsers.add_parser(
        'finalize_session',
        help='Finalize the active session and write markdown file'
    )
    finalize_parser.add_argument('--key-context', required=True, help='Key context summary for this session')

    # relate command
    relate_parser = subparsers.add_parser(
        'relate',
        help='Create a RELATES_TO edge between two nodes'
    )
    relate_parser.add_argument('node1', help='First node ID')
    relate_parser.add_argument('node2', help='Second node ID')

    # resolve_question command
    resolve_parser = subparsers.add_parser(
        'resolve_question',
        help='Create a RESOLVED_BY edge from question to session'
    )
    resolve_parser.add_argument('question_id', help='Question node ID')
    resolve_parser.add_argument('--session', required=True, help='Session node ID')

    # import_source command
    import_parser = subparsers.add_parser(
        'import_source',
        help='Import a file as a source node'
    )
    import_parser.add_argument('file_path', help='Path to file to import')
    import_parser.add_argument('--name', required=True, help='Name for the source node')
    import_parser.add_argument('--relates-to', help='Comma-separated node IDs to relate to')

    # memory_init command
    subparsers.add_parser(
        'memory_init',
        help='Initialize the Karpathy-style LLM Wiki layer'
    )

    # memory_capture command
    memory_capture_parser = subparsers.add_parser(
        'memory_capture',
        help='Capture a raw source into the LLM Wiki and graph'
    )
    memory_capture_parser.add_argument('title', help='Source title')
    memory_capture_parser.add_argument('--url', help='Original URL')
    memory_capture_parser.add_argument('--file', help='Path to a local text/markdown source')
    memory_capture_parser.add_argument('--text', help='Inline source text')
    memory_capture_parser.add_argument('--stdin', action='store_true',
                                       help='Read source text from stdin')
    memory_capture_parser.add_argument('--notes', help='Capture notes or why this source matters')
    memory_capture_parser.add_argument('--kind', default='note',
                                       help='Source kind, e.g. tweet, blog, chat, doc, note')
    memory_capture_parser.add_argument('--threads',
                                       help='Comma-separated thread names to link')
    memory_capture_parser.add_argument('--tags',
                                       help='Comma-separated tags')
    memory_capture_parser.add_argument('--fetch', action='store_true',
                                       help='Best-effort fetch of URL text')

    # memory_compile command
    subparsers.add_parser(
        'memory_compile',
        help='Compile graph memory into LLM Wiki markdown pages'
    )

    # memory_search command
    memory_search_parser = subparsers.add_parser(
        'memory_search',
        help='Search raw and compiled LLM Wiki markdown'
    )
    memory_search_parser.add_argument('term', help='Search term')
    memory_search_parser.add_argument('--raw', action='store_true',
                                      help='Search raw sources only')
    memory_search_parser.add_argument('--wiki', action='store_true',
                                      help='Search compiled wiki only')
    memory_search_parser.add_argument('--limit', type=int, default=20,
                                      help='Maximum matches to show')

    # memory_lint command
    memory_lint_parser = subparsers.add_parser(
        'memory_lint',
        help='Health-check the graph and LLM Wiki layer'
    )
    memory_lint_parser.add_argument('--strict', action='store_true',
                                    help='Exit non-zero when issues are found')

    # confidence command
    confidence_parser = subparsers.add_parser(
        'confidence',
        help='Display confidence scores'
    )
    confidence_parser.add_argument('thread_name', nargs='?', default=None,
                                    help='Optional: thread name for detailed view')

    # Parse arguments
    args = parser.parse_args()

    # Dispatch to appropriate command
    if args.command == 'start_session':
        cmd_start_session(args, db)
    elif args.command == 'log_decision':
        cmd_log_decision(args, db)
    elif args.command == 'log_question':
        cmd_log_question(args, db)
    elif args.command == 'log_shift':
        cmd_log_shift(args, db)
    elif args.command == 'create_thread':
        cmd_create_thread(args, db)
    elif args.command == 'recent':
        cmd_recent(args, db)
    elif args.command == 'get_context':
        cmd_get_context(args, db)
    elif args.command == 'get_thread':
        cmd_get_thread(args, db)
    elif args.command == 'open_questions':
        cmd_open_questions(args, db)
    elif args.command == 'finalize_session':
        cmd_finalize_session(args, db)
    elif args.command == 'relate':
        cmd_relate(args, db)
    elif args.command == 'resolve_question':
        cmd_resolve_question(args, db)
    elif args.command == 'import_source':
        cmd_import_source(args, db)
    elif args.command == 'memory_init':
        cmd_memory_init(args, db)
    elif args.command == 'memory_capture':
        cmd_memory_capture(args, db)
    elif args.command == 'memory_compile':
        cmd_memory_compile(args, db)
    elif args.command == 'memory_search':
        cmd_memory_search(args, db)
    elif args.command == 'memory_lint':
        cmd_memory_lint(args, db)
    elif args.command == 'confidence':
        cmd_confidence(args, db)
    elif args.command is None:
        parser.print_help()
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)

    db.close()
