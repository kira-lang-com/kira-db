# KiraDB

KiraDB is an embedded database for Kira: one ordered key-value store with two
faces. The document face stores documents in collections by key; the table
face stores typed tables, rows and indexes on top of it. Schemas, rows and
queries are Kira values checked against the table's schema, never text, so
nothing can be injected into a query.

The same engine runs embedded over a local directory, as a sync server whose
clients each hold a replica, and as a member of a replication group that
keeps one database of record on several servers.

[`docs/SPEC.md`](docs/SPEC.md) is the rule for every layer:

| Layer | Directory |
| --- | --- |
| Values and their binary codec | `app/Values` |
| Order-preserving keys | `app/Keys` |
| Log, memory table, sorted tables, compaction | `app/Storage` |
| `Database` and `Collection`, the document API | `app/Documents` |
| Durable append-only streams | `app/Streams` |
| Schemas, typed rows, indexes and queries | `app/Tables` |
| The sync server, replicas and replication | `app/Sync` |

## Use it

Add KiraDB to a package's dependencies in `package.kira`, then build and
test with the Kira toolchain:

```sh
kira build
kira test
```

`RELEASE.md` holds the current week's notes. KiraDB is developed in the
Tessera monorepo and mirrored here by Cairn.
