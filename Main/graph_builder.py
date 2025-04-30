import os
import warnings
from functools import lru_cache
import colorsys
import threading
import time
from queue import Queue

import osmnx as ox
import networkx as nx
from shapely.geometry import Point, LineString
from PIL import Image, ImageDraw, ImageTk

from Main.Algorithms.pathAlgorithms import PathAlgorithms

ox.settings.timeout = 300
MAX_PATHS = 100
MAX_NODES = 10000
PATH_SIMPLIFICATION_TOLERANCE = 0.0001  # Degrees
# Viewport-based rendering settings
MAX_EDGES_PER_BATCH = 500
EDGE_BATCH_DELAY = 0.05  # seconds between batches
EDGE_VISIBILITY_THRESHOLD = 200  # maximum number of edges visible at once

# Define neon green color
NEON_GREEN = "#39FF14"  # Bright neon green


@lru_cache(maxsize=None)
def _load_graphml_cached(path):
    return ox.load_graphml(path)


def load_or_build_graph(center_point, radius, graphml_path, network_type="drive"):  # returns an OSMnx graph
    if os.path.exists(graphml_path):
        return _load_graphml_cached(graphml_path)
    G = ox.graph_from_point(center_point, dist=radius,
                            network_type=network_type, simplify=True)
    if len(G.nodes) > MAX_NODES:
        raise MemoryError(f"Graph exceeds safety limit of {MAX_NODES} nodes")
    ox.save_graphml(G, graphml_path)
    _load_graphml_cached.cache_clear()
    return _load_graphml_cached(graphml_path)


def _enumerate_k_paths(G_simple, source, target, k, weight):
    if k is None:
        k = MAX_PATHS
    try:
        gen = nx.shortest_simple_paths(G_simple, source, target, weight=weight)
    except nx.NetworkXNoPath:
        return []
    return [next(gen) for _ in range(k)]


class OptimizedGraphBuilder:
    def __init__(self, map_widget, cache_dir='graphs', default_weight='length'):
        self.map_widget = map_widget
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

        self.G_proj = None
        self.G_simple = None
        self.edge_coords = []
        self.orig_node = None
        self.dest_node = None
        self.node_coords = {}
        self.highlight_objects = []
        self.edge_objects = []
        self.node_objects = []
        self.node_labels = []
        self.distance_markers = []
        # hold onto PhotoImage refs so they don't get GC'd
        self._marker_icons = []

        self.weight = default_weight
        self.proj_s = None
        self.proj_e = None
        self.path_cache = {}
        self.algo_steps = []
        self.algorithm_results = {}

        # Progressive loading state
        self.loading_edges = False
        self.edge_queue = Queue()
        self.visible_edges = set()
        self.edge_batch_lock = threading.Lock()

        # Add node ID mapping
        self.node_id_map = {}  # Maps original node IDs to sequential IDs starting from 1
        self.inverse_node_map = {}  # Maps sequential IDs back to original node IDs

        # Ensure node 1 is the starting point
        self.starting_node = None  # Will be set during graph building

        # Bind map events for viewport-based rendering
        self._setup_map_bindings()

        # Store spatial index for edges (for viewport optimization)
        self.edge_spatial_index = {}

    def _setup_map_bindings(self):
        """Set up event bindings for viewport-based rendering"""
        canvas = self.map_widget.canvas
        canvas.bind("<Configure>", self._on_viewport_change)
        canvas.bind("<ButtonRelease-1>", self._on_viewport_change)
        canvas.bind("<MouseWheel>", self._on_viewport_change)
        canvas.bind("<Button-4>", self._on_viewport_change)
        canvas.bind("<Button-5>", self._on_viewport_change)
        self._last_viewport_update = 0

    def _on_viewport_change(self, event):
        """Handle map viewport changes - update visible edges"""
        current_time = time.time()
        if current_time - self._last_viewport_update < 0.2:
            return
        self._last_viewport_update = current_time
        self.map_widget.after(200, self._update_visible_edges)

    def _update_visible_edges(self):
        """Update which edges are visible based on the current map viewport."""
        if not self.edge_coords:
            return

        try:
            # 1) If a get_bounds() method exists, use it:
            if hasattr(self.map_widget, "get_bounds"):
                northeast, southwest = self.map_widget.get_bounds()

            # 2) Otherwise, fall back to converting canvas corners:
            elif hasattr(self.map_widget, "convert_canvas_coords_to_decimal_coords"):
                canvas = self.map_widget.canvas
                w, h = canvas.winfo_width(), canvas.winfo_height()
                ne_lat, ne_lon = self.map_widget.convert_canvas_coords_to_decimal_coords(w, 0)
                sw_lat, sw_lon = self.map_widget.convert_canvas_coords_to_decimal_coords(0, h)
                northeast, southwest = (ne_lat, ne_lon), (sw_lat, sw_lon)

            # 3) If neither is available, we can't proceed:
            else:
                # If we can't determine the viewport, show all edges
                self._display_all_edges()
                return

            lat_n, lon_e = northeast
            lat_s, lon_w = southwest

            visible = set()
            for edge_id, (start, end) in enumerate(self.edge_coords):
                lat1, lon1 = start
                lat2, lon2 = end
                if (min(lat1, lat2) <= lat_n and max(lat1, lat2) >= lat_s and
                        min(lon1, lon2) <= lon_e and max(lon1, lon2) >= lon_w):
                    visible.add(edge_id)

            # Ensure we're showing a minimum number of edges for better visualization
            # If very few edges are visible, show more edges to ensure graph connectivity is visible
            if len(visible) < 20 and self.edge_coords:
                # Add more edges to make sure graph structure is visible
                self._display_all_edges()
                return

            to_add = visible - self.visible_edges
            to_remove = self.visible_edges - visible

            if len(to_add) > 0 or len(to_remove) > 10:
                self._update_edge_visibility(to_add, to_remove)
                self.visible_edges = visible

        except Exception as e:
            warnings.warn(f"Error updating visible edges: {e}")
            # Fallback to displaying all edges
            self._display_all_edges()

    def _display_all_edges(self):
        """Fallback method to display all edges when viewport-based rendering fails"""
        with self.edge_batch_lock:
            for i, obj in enumerate(self.edge_objects):
                if obj:
                    try:
                        obj.delete()
                    except Exception:
                        pass
            self.edge_objects = [None] * len(self.edge_coords)

            # Limit to a reasonable number of edges to avoid performance issues
            max_edges = min(len(self.edge_coords), 1000)
            step = max(1, len(self.edge_coords) // max_edges)

            for edge_id in range(0, len(self.edge_coords), step):
                if edge_id < len(self.edge_coords):
                    u, v = self.edge_coords[edge_id]
                    try:
                        # Use neon green for all edges
                        path_obj = self.map_widget.set_path([u, v], color=NEON_GREEN, width=1)
                        if edge_id >= len(self.edge_objects):
                            self.edge_objects.extend([None] * (edge_id - len(self.edge_objects) + 1))
                        self.edge_objects[edge_id] = path_obj
                        self.visible_edges.add(edge_id)
                    except Exception as e:
                        warnings.warn(f"Error drawing edge {edge_id}: {e}")

    def _update_edge_visibility(self, to_add, to_remove):
        """Update which edges are displayed on the map"""
        with self.edge_batch_lock:
            for edge_id in to_remove:
                if edge_id < len(self.edge_objects) and self.edge_objects[edge_id]:
                    try:
                        self.edge_objects[edge_id].delete()
                        self.edge_objects[edge_id] = None
                    except Exception:
                        pass

            edges_to_add = list(to_add)[:EDGE_VISIBILITY_THRESHOLD]
            for edge_id in edges_to_add:
                if edge_id < len(self.edge_coords):
                    u, v = self.edge_coords[edge_id]
                    try:
                        if edge_id >= len(self.edge_objects):
                            self.edge_objects.extend([None] * (edge_id - len(self.edge_objects) + 1))
                        # Use neon green for all edges
                        path_obj = self.map_widget.set_path([u, v], color=NEON_GREEN, width=1)
                        self.edge_objects[edge_id] = path_obj
                    except Exception as e:
                        warnings.warn(f"Error adding edge {edge_id}: {e}")

    def clear_highlights(self):
        """Remove all paths, edges and node markers."""
        for obj in self.highlight_objects + self.distance_markers + self.node_objects + self.node_labels:
            try:
                obj.delete()
            except Exception:
                pass

        with self.edge_batch_lock:
            for i, obj in enumerate(self.edge_objects):
                if obj:
                    try:
                        obj.delete()
                    except Exception:
                        pass
            self.edge_objects = []

        self._marker_icons.clear()
        self.highlight_objects.clear()
        self.distance_markers.clear()
        self.node_objects.clear()
        self.node_labels.clear()
        self.visible_edges.clear()
        self.path_cache.clear()
        self.algorithm_results.clear()

        self.loading_edges = False
        while not self.edge_queue.empty():
            try:
                self.edge_queue.get_nowait()
                self.edge_queue.task_done()
            except Exception:
                pass

    def _make_circle_icon(self, color, size=6):
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((0, 0, size - 1, size - 1), fill=color, outline=color)
        return ImageTk.PhotoImage(img)

    def highlight_paths(self, paths, width=3):
        for obj in self.highlight_objects + self.distance_markers:
            try:
                obj.delete()
            except Exception:
                pass
        self.highlight_objects.clear()
        self.distance_markers.clear()
        self._marker_icons.clear()

        simplified_paths = []
        for path in paths:
            if len(path) < 2:
                continue
            line = LineString([self.node_coords[n] for n in path])
            simp = line.simplify(PATH_SIMPLIFICATION_TOLERANCE)
            if simp.geom_type == 'LineString':
                simplified_paths.append(list(simp.coords))
            else:
                for part in getattr(simp, 'geoms', [line]):
                    simplified_paths.append(list(part.coords))

        for coords in simplified_paths:
            p = self.map_widget.set_path(coords, color="#0000FF", width=width)
            self.highlight_objects.append(p)

        bg = self.map_widget.canvas.cget('bg')
        if isinstance(bg, str) and bg.startswith('#') and len(bg) == 7:
            r_bg = int(bg[1:3], 16)
            g_bg = int(bg[3:5], 16)
            b_bg = int(bg[5:7], 16)
            lum = (0.299 * r_bg + 0.587 * g_bg + 0.114 * b_bg) / 255
            text_color = '#FFFFFF' if lum < 0.5 else '#000000'
        else:
            text_color = '#FFFFFF'

        for idx_path, coords in enumerate(simplified_paths):
            hue = (idx_path * 0.618033988749895) % 1.0
            r, g, b = colorsys.hls_to_rgb(hue, 0.5, 1.0)
            node_color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
            icon = self._make_circle_icon(node_color, size=6)
            self._marker_icons.append(icon)

            step = max(1, len(coords) // 10)
            for i in range(step, len(coords), step):
                prev_lat, prev_lon = coords[i - step]
                curr_lat, curr_lon = coords[i]
                dist_m = ox.distance.great_circle(prev_lat, prev_lon,
                                                  curr_lat, curr_lon)
                label = (f"{dist_m:.0f} m"
                         if dist_m < 1000
                         else f"{dist_m / 1000:.2f} km")

                m = self.map_widget.set_marker(
                    curr_lat,
                    curr_lon,
                    text=label,
                    font=("Helvetica", 8, "bold"),
                    icon=icon,
                    icon_anchor="center",
                    text_color=text_color
                )
                self.distance_markers.append(m)

        # Add node markers for the path nodes
        if paths and paths[0]:
            path = paths[0]  # Use the first path for node labels
            for node in path:
                lat, lon = self.node_coords[node]
                # Create a node label with its sequential ID
                seq_id = self.node_id_map.get(node, 0)
                node_label = f"Node {seq_id}"
                m = self.map_widget.set_marker(
                    lat, lon,
                    text=node_label,
                    font=("Helvetica", 10, "bold"),
                    text_color=text_color,
                    marker_color_circle="blue",
                    marker_color_outside="white",
                    icon_anchor="center"
                )
                self.node_labels.append(m)

        return self.highlight_objects + self.distance_markers + self.node_labels

    def display_node_labels(self):
        """Display labels for all nodes in the graph"""
        for obj in self.node_objects + self.node_labels:
            try:
                obj.delete()
            except Exception:
                pass
        self.node_objects.clear()
        self.node_labels.clear()

        # Get text color based on background
        bg = self.map_widget.canvas.cget('bg')
        if isinstance(bg, str) and bg.startswith('#') and len(bg) == 7:
            r_bg = int(bg[1:3], 16)
            g_bg = int(bg[3:5], 16)
            b_bg = int(bg[5:7], 16)
            lum = (0.299 * r_bg + 0.587 * g_bg + 0.114 * b_bg) / 255
            text_color = '#FFFFFF' if lum < 0.5 else '#000000'
        else:
            text_color = '#FFFFFF'

        # Create a larger icon for the starting node (Node 1)
        start_node_icon = self._make_circle_icon("red", size=12)
        # Create a smaller icon for other nodes
        node_icon = self._make_circle_icon("orange", size=8)
        self._marker_icons.append(start_node_icon)
        self._marker_icons.append(node_icon)

        # Limit the number of nodes to display to avoid performance issues
        visible_nodes = set()

        # Get current viewport
        if hasattr(self.map_widget, "get_bounds"):
            northeast, southwest = self.map_widget.get_bounds()
            lat_n, lon_e = northeast
            lat_s, lon_w = southwest

            # Filter nodes to those in the viewport
            for node, (lat, lon) in self.node_coords.items():
                if (lat <= lat_n and lat >= lat_s and lon <= lon_e and lon >= lon_w):
                    visible_nodes.add(node)
        else:
            # If we can't get bounds, limit to a reasonable number
            visible_nodes = set(list(self.node_coords.keys())[:100])

        # Ensure the starting node (Node 1) is always included
        if self.starting_node and self.starting_node in self.node_coords:
            visible_nodes.add(self.starting_node)

        # Add node markers with sequential labels
        for node in visible_nodes:
            lat, lon = self.node_coords[node]
            seq_id = self.node_id_map.get(node, 0)

            # Determine if this is the starting node (Node 1)
            is_starting_node = (seq_id == 1)

            font_size = 10 if is_starting_node else 8
            icon = start_node_icon if is_starting_node else node_icon

            m = self.map_widget.set_marker(
                lat, lon,
                text=f"Node {seq_id}",
                font=("Helvetica", font_size, "bold" if is_starting_node else "normal"),
                text_color=text_color,
                icon=icon,
                icon_anchor="center"
            )
            self.node_labels.append(m)

        return self.node_labels

    def build_graph(self, start, end, use_bbox=False, buffer=1000):
        self.clear_highlights()

        if len(start) != 2 or len(end) != 2:
            raise ValueError("Start and end must be (lat,lon) tuples")
        lat1, lon1 = start
        lat2, lon2 = end
        straight = ox.distance.great_circle(lat1, lon1, lat2, lon2)
        radius = straight / 2 + buffer
        center = ((lat1 + lat2) / 2, (lon1 + lon2) / 2)
        fname = os.path.join(self.cache_dir,
                             f"pt_{center[0]:.5f}_{center[1]:.5f}_{radius:.0f}.graphml")
        G0 = load_or_build_graph(center, radius, fname)
        G0 = ox.distance.add_edge_lengths(G0)
        Gp = ox.project_graph(G0)

        self.node_coords = {n: (d['y'], d['x'])
                            for n, d in G0.nodes(data=True)}

        # Find the node closest to the start point - this will be assigned ID 1
        self.proj_s = ox.projection.project_geometry(Point(lon1, lat1), crs=G0.graph.get('crs'))[0]
        orig_node = ox.distance.nearest_nodes(Gp, X=self.proj_s.x, Y=self.proj_s.y)

        # Create sequential node ID mapping starting from 1, with orig_node as ID 1
        nodes = list(G0.nodes())
        # Remove orig_node from the list if it exists
        if orig_node in nodes:
            nodes.remove(orig_node)
        # Create the mapping with orig_node as ID 1
        self.node_id_map = {orig_node: 1}
        # Add the rest of the nodes with sequential IDs starting from 2
        self.node_id_map.update({node: i + 2 for i, node in enumerate(nodes)})

        # Create inverse mapping (from sequential ID to original node ID)
        self.inverse_node_map = {v: k for k, v in self.node_id_map.items()}

        # Keep track of the starting node
        self.starting_node = orig_node

        self.edge_coords = [
            ((G0.nodes[u]['y'], G0.nodes[u]['x']),
             (G0.nodes[v]['y'], G0.nodes[v]['x']))
            for u, v in G0.edges()
        ]

        self.edge_objects = [None] * len(self.edge_coords)

        crs = G0.graph.get('crs')
        self.proj_s = ox.projection.project_geometry(Point(lon1, lat1), crs=crs)[0]
        self.proj_e = ox.projection.project_geometry(Point(lon2, lat2), crs=crs)[0]
        self.orig_node = ox.distance.nearest_nodes(Gp,
                                                   X=self.proj_s.x,
                                                   Y=self.proj_s.y)
        self.dest_node = ox.distance.nearest_nodes(Gp,
                                                   X=self.proj_e.x,
                                                   Y=self.proj_e.y)
        self.G_proj = Gp
        self.G_simple = ox.convert.to_digraph(Gp, weight=self.weight)

        # Force display all edges initially for better visualization
        self._display_all_edges()

        return self

    def _process_edge_queue(self):
        if not self.loading_edges:
            return

        with self.edge_batch_lock:
            edges_to_draw = []
            for _ in range(min(MAX_EDGES_PER_BATCH, self.edge_queue.qsize())):
                if not self.loading_edges:
                    break
                try:
                    edge_id = self.edge_queue.get_nowait()
                    edges_to_draw.append(edge_id)
                    self.edge_queue.task_done()
                except Exception:
                    break

            for edge_id in edges_to_draw:
                if edge_id in self.visible_edges and edge_id < len(self.edge_coords):
                    u, v = self.edge_coords[edge_id]
                    try:
                        if edge_id >= len(self.edge_objects):
                            self.edge_objects.extend([None] * (edge_id - len(self.edge_objects) + 1))
                        # Use neon green for all edges
                        path_obj = self.map_widget.set_path([u, v], color=NEON_GREEN, width=1)
                        self.edge_objects[edge_id] = path_obj
                    except Exception:
                        pass

        if self.loading_edges and not self.edge_queue.empty():
            self.map_widget.after(int(EDGE_BATCH_DELAY * 1000), self._process_edge_queue)

    def display_graph(self, color=NEON_GREEN, width=1):
        with self.edge_batch_lock:
            for i, obj in enumerate(self.edge_objects):
                if obj:
                    try:
                        obj.delete()
                    except Exception:
                        pass
            self.edge_objects = [None] * len(self.edge_coords)
            self.visible_edges.clear()

        self.loading_edges = True

        # Initially force display all edges
        self._display_all_edges()

        # Queue remaining edges for progressive loading if needed
        for edge_id in range(len(self.edge_coords)):
            if edge_id not in self.visible_edges:
                self.edge_queue.put(edge_id)

        self._process_edge_queue()

        # Display node labels
        self.display_node_labels()

        return self.edge_objects

    def find_k_paths(self, k=5, weight=None):
        if not all([self.G_simple, self.orig_node, self.dest_node]):
            raise RuntimeError("Graph not initialized")
        key = (self.orig_node, self.dest_node, k, weight or self.weight)
        if key not in self.path_cache:
            self.path_cache[key] = _enumerate_k_paths(
                self.G_simple, self.orig_node, self.dest_node,
                k, weight or self.weight)
        return self.path_cache[key]

    def run_dijkstra(self):
        """Run Dijkstra's algorithm and format the explanation"""
        if not all([self.G_simple, self.orig_node, self.dest_node]):
            raise RuntimeError("Graph not initialized")

        # Run Dijkstra with step tracking
        dist, prev, steps = PathAlgorithms.dijkstra(
            self.G_simple, self.orig_node, self.dest_node, self.weight)

        # Reconstruct path
        path = PathAlgorithms.reconstruct_path(prev, self.orig_node, self.dest_node)

        # Generate explanation with sequential node IDs
        explanation = self._format_steps_explanation_with_seq_ids(
            self.G_simple, steps, prev, self.orig_node, self.dest_node)

        # Store results
        self.algorithm_results["dijkstra"] = {
            "path": path,
            "dist": dist,
            "prev": prev,
            "steps": steps,
            "explanation": explanation
        }

        # Highlight the path
        if path:
            self.highlight_paths([path])

        return path, explanation

    def run_bellman_ford(self):
        """Run Bellman-Ford algorithm and format the explanation"""
        if not all([self.G_simple, self.orig_node, self.dest_node]):
            raise RuntimeError("Graph not initialized")

        # Run Bellman-Ford with step tracking
        dist, prev, steps = PathAlgorithms.bellman_ford(
            self.G_simple, self.orig_node, self.dest_node, self.weight)

        # Check if a path was found (could be None if negative cycle was detected)
        if dist is None or prev is None:
            explanation = "Negative cycle detected in the graph. Cannot find shortest path."
            path = []
        else:
            # Reconstruct path
            path = PathAlgorithms.reconstruct_path(prev, self.orig_node, self.dest_node)

            # Generate explanation with sequential node IDs
            explanation = self._format_steps_explanation_with_seq_ids(
                self.G_simple, steps, prev, self.orig_node, self.dest_node)

        # Store results
        self.algorithm_results["bellman_ford"] = {
            "path": path,
            "dist": dist,
            "prev": prev,
            "steps": steps,
            "explanation": explanation
        }

        # Highlight the path
        if path:
            self.highlight_paths([path])

        return path, explanation

    def _format_steps_explanation_with_seq_ids(self, G, steps, prev, source, target):
        """Format algorithm steps with sequential node IDs for better readability"""
        if not steps:
            return "No steps recorded during algorithm execution."

        explanation = []
        explanation.append(f"Starting from Node {self.node_id_map.get(source, '?')} (original ID: {source})")

        for step_num, step in enumerate(steps, 1):
            if isinstance(step, dict):  # Distance update
                step_details = []
                for node, (dist, prev_node) in step.items():
                    seq_node_id = self.node_id_map.get(node, '?')
                    seq_prev_id = self.node_id_map.get(prev_node, '?') if prev_node is not None else 'None'

                    step_details.append(
                        f"Updated Node {seq_node_id}: distance = {dist:.2f}, previous = Node {seq_prev_id}")

                explanation.append(f"Step {step_num}:")
                explanation.extend([f"  {detail}" for detail in sorted(step_details)])
            elif isinstance(step, tuple) and len(step) == 3:  # Node selection (Dijkstra)
                node, dist, _ = step
                seq_node_id = self.node_id_map.get(node, '?')
                explanation.append(f"Step {step_num}: Selected Node {seq_node_id} with distance {dist:.2f}")

        # Reconstruct the path using sequential IDs
        if target in prev or target == source:
            path = []
            at = target
            while at is not None:
                path.append(at)
                at = prev.get(at)
            path.reverse()

            path_str = " → ".join([f"Node {self.node_id_map.get(n, '?')}" for n in path])
            total_dist = sum(G[u][v].get(self.weight, 0) for u, v in zip(path[:-1], path[1:]))

            explanation.append(f"\nFinal shortest path ({total_dist:.2f} units):")
            explanation.append(path_str)
        else:
            explanation.append(
                f"\nNo path found from Node {self.node_id_map.get(source, '?')} to Node {self.node_id_map.get(target, '?')}")

        return "\n".join(explanation)