# The same workloads as app/main.kira, on SQLite with the same durability:
# WAL, synchronous=FULL, one sync per durable write.
import os, shutil, sqlite3, time

ROOT = os.environ.get("BENCH_ROOT", "/tmp/kiradb-bench-sqlite")
DURABLE, BULK, LOOKUPS, INDEXED = 2000, 100000, 100000, 1000


def report(name, started, operations):
    seconds = time.perf_counter() - started
    print(f"{name} {round(operations / seconds)}/s {round(seconds * 1000)}ms")


def fresh(name):
    path = os.path.join(ROOT, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    db = sqlite3.connect(os.path.join(path, "db"), isolation_level=None)
    db.execute("pragma journal_mode=wal")
    db.execute("pragma synchronous=full")
    return db


def documents():
    db = fresh("documents")
    db.execute("create table items (key integer primary key, name text, price real, stock integer)")
    row = lambda k: (k, f"item {k}", k * 0.25, k % 97)
    started = time.perf_counter()
    for k in range(DURABLE):
        db.execute("insert or replace into items values (?, ?, ?, ?)", row(k))
    report("durable puts", started, DURABLE)
    started = time.perf_counter()
    db.execute("begin")
    db.executemany("insert or replace into items values (?, ?, ?, ?)", (row(k) for k in range(DURABLE, DURABLE + BULK)))
    db.execute("commit")
    report("bulk puts", started, BULK)
    started = time.perf_counter()
    found = 0
    for i in range(LOOKUPS):
        if db.execute("select * from items where key = ?", ((i * 7919) % (DURABLE + BULK),)).fetchone():
            found += 1
    report("point gets", started, LOOKUPS)
    started = time.perf_counter()
    rows = db.execute("select * from items order by key").fetchall()
    report("full scan", started, len(rows))
    assert found == LOOKUPS


def tables():
    db = fresh("tables")
    db.execute("create table items (id integer primary key, name text not null, stock integer not null)")
    db.execute("create index byStock on items (stock)")
    started = time.perf_counter()
    for i in range(DURABLE):
        db.execute("insert into items values (?, ?, ?)", (i, f"item {i}", i % 97))
    report("durable inserts", started, DURABLE)
    started = time.perf_counter()
    for i in range(INDEXED):
        db.execute("select * from items where stock = ?", (i % 97,)).fetchall()
    report("indexed selects", started, INDEXED)


documents()
tables()
