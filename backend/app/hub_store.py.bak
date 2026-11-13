"""Bounded SQLite POI pool. Locations are seeds, never parking certifications."""
import math
from pathlib import Path
import sqlite3
from time import time

from .geometry import haversine_m
from .models import Place, Point


class HubStore:
    def __init__(self, path=None, ttl=30 * 24 * 3600):
        self.path = Path(path) if path else Path(__file__).resolve().parents[2] / '.runtime' / 'hub-pool.sqlite3'
        self.ttl = ttl

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=.2)
        db.executescript('''
            CREATE TABLE IF NOT EXISTS hubs (
                id INTEGER PRIMARY KEY, namespace TEXT, kind TEXT, identity TEXT,
                payload TEXT, expires REAL, UNIQUE(namespace,kind,identity));
            CREATE VIRTUAL TABLE IF NOT EXISTS hub_geo USING rtree(id,min_lon,max_lon,min_lat,max_lat);
            CREATE TABLE IF NOT EXISTS queries (
                namespace TEXT, kind TEXT, lon REAL, lat REAL, radius REAL, expires REAL,
                PRIMARY KEY(namespace,kind,lon,lat,radius));
        ''')
        return db

    def lookup(self, namespace, center, kind, radius):
        try:
            db = self.connect()
            try:
                dx = radius / max(1000, 111000 * math.cos(math.radians(center.lat)))
                dy = radius / 110000
                rows = db.execute('''SELECT h.payload FROM hubs h JOIN hub_geo g ON h.id=g.id
                    WHERE h.namespace=? AND h.kind=? AND h.expires>?
                    AND g.min_lon<=? AND g.max_lon>=? AND g.min_lat<=? AND g.max_lat>=?''',
                    (namespace, kind, time(), center.lon+dx, center.lon-dx, center.lat+dy, center.lat-dy)).fetchall()
                places = [Place.model_validate_json(row[0]) for row in rows]
                places = sorted((p for p in places if haversine_m(center, p.point) <= radius),
                                key=lambda p: (haversine_m(center, p.point), p.point.csv()))
                queries = db.execute('SELECT lon,lat,radius FROM queries WHERE namespace=? AND kind=? AND expires>?',
                                     (namespace, kind, time())).fetchall()
                covered = any(haversine_m(center, Point(lon=x, lat=y)) + radius <= r + 1 for x,y,r in queries)
                return places, covered
            finally:
                db.close()
        except (OSError, sqlite3.Error, ValueError):
            return [], False

    def put(self, namespace, center, kind, radius, places):
        try:
            db = self.connect()
            try:
                with db:
                    now = time()
                    db.execute('DELETE FROM queries WHERE expires<=?', (now,))
                    db.execute('DELETE FROM hubs WHERE expires<=?', (now,))
                    for place in places:
                        identity = (place.poi_id or '') + ':' + place.point.csv()
                        db.execute('''INSERT INTO hubs(namespace,kind,identity,payload,expires) VALUES(?,?,?,?,?)
                            ON CONFLICT(namespace,kind,identity) DO UPDATE SET payload=excluded.payload,expires=excluded.expires''',
                            (namespace, kind, identity, place.model_dump_json(), now+self.ttl))
                        row_id = db.execute('SELECT id FROM hubs WHERE namespace=? AND kind=? AND identity=?',
                                            (namespace, kind, identity)).fetchone()[0]
                        db.execute('INSERT OR REPLACE INTO hub_geo VALUES(?,?,?,?,?)',
                                   (row_id, place.point.lon, place.point.lon, place.point.lat, place.point.lat))
                    db.execute('INSERT OR REPLACE INTO queries VALUES(?,?,?,?,?,?)',
                               (namespace, kind, center.lon, center.lat, radius, now+self.ttl))
                    # Eviction also invalidates query coverage, so discarded POIs can be fetched again.
                    count = db.execute('SELECT COUNT(*) FROM hubs').fetchone()[0]
                    if count > 20000:
                        db.execute('DELETE FROM hubs WHERE id IN (SELECT id FROM hubs ORDER BY expires LIMIT ?)', (count-20000,))
                        db.execute('DELETE FROM queries')
                    db.execute('DELETE FROM hub_geo WHERE id NOT IN (SELECT id FROM hubs)')
                    db.execute('DELETE FROM queries WHERE rowid IN (SELECT rowid FROM queries ORDER BY expires DESC LIMIT -1 OFFSET 2000)')
            finally:
                db.close()
        except (OSError, sqlite3.Error):
            pass  # The existing provider path remains usable if storage is unavailable.


hub_store = HubStore()
