# KiraDB specification

KiraDB is one database with two faces. The document face stores documents in
collections by key. The table face stores typed tables, rows and indexes, and
is built entirely on the document face's ordered key-value core. Nothing below
the key-value core knows tables exist.

KiraDB has no query language. Schemas, rows and queries are Kira values,
checked against the table's schema before they touch storage: a query is
never text, so nothing can be injected into one.

The same engine runs embedded in one process over a local directory, as a
sync server whose clients each hold a replica (section 8), as a member of a
replication group that keeps one database of record on several servers
(section 9), and, later, as the
storage of each node in a cluster that splits the key space into ranges. This document is the rule for every layer it covers. A layer it does
not describe does not exist yet.

## 1. Language rules

KiraDB is Kira, written to every rule of the `working-with-kira` skill. In
addition:

- Numbers are `Number`, `UNumber`, `N8 N16 N32` and `UN8 UN16 UN32` only.
  No fixed-width integer or float type appears in KiraDB.
- A byte is `UN8`. Bytes are `[UN8]`.
- KiraDB reaches the operating system only through Foundation. When
  Foundation lacks a capability, it is added to klf-kira as a kernel service,
  never as a system call.

## 2. Layers

| Layer | Directory | Knows about |
| --- | --- | --- |
| Values | `app/Values` | `Value`, `Document`, their binary codec |
| Keys | `app/Keys` | Order-preserving key encoding of values |
| Storage | `app/Storage` | Log, memory table, sorted tables, manifest, compaction |
| Documents | `app/Documents` | `Database`, `Collection`: the public document API |
| Tables | `app/Tables` | Schemas, catalog, typed rows, indexes, queries and their plans |
| Sync | `app/Sync` | Functions, the sync server, replicas, their protocol and its QUIC transport |
| Replication | `app/Replication` | Groups of servers holding one database of record: log, elections, snapshots |

Each layer uses only the layers above it in this table.

## 3. Values

```kira
enum Value {
    Null
    Bool(Bool)
    Number(Number)
    Text(String)
    Bytes([UN8])
    List([Value])
    Document(Document)
}
```

A `Document` is an ordered list of `(String, Value)` fields; a field name
appears at most once.

### 3.1 Value codec

A value is written as one tag byte and its payload. A length or count is an
unsigned variable-length number (section 3.2).

| Tag | Value | Payload |
| --- | --- | --- |
| 0 | `Null` | none |
| 1 | `Bool(false)` | none |
| 2 | `Bool(true)` | none |
| 3 | `Number` | its decimal text (section 3.3) as a length-prefixed byte string |
| 4 | `Text` | length, then UTF-8 bytes |
| 5 | `Bytes` | length, then bytes |
| 6 | `List` | count, then each value |
| 7 | `Document` | count, then each field: name as length and UTF-8 bytes, then the value |

### 3.2 Variable-length numbers

A non-negative whole number is written seven bits per byte, low bits first;
every byte but the last has its top bit (128) set.

### 3.3 Number text

A `Number` is written as `String(number)` and read back with `Number(text)`:
the exact decimal round-trips.

## 4. Keys

A key is `[UN8]`. Keys order byte by byte; a key that is a prefix of another
orders first. Values encode into keys so that byte order is value order:

| Tag | Value | Body |
| --- | --- | --- |
| 1 | `Null` | none |
| 2 | `false` | none |
| 3 | `true` | none |
| 16 | negative `Number` | the positive body of its magnitude, every byte complemented (`255 - byte`) |
| 17 | zero | none |
| 18 | positive `Number` | exponent, then digits |
| 32 | `Text` | UTF-8 bytes escaped, then terminator |
| 48 | `Bytes` | bytes escaped, then terminator |

A positive number `0.d1 d2 ... dn x 10^e`, `d1` not zero and `dn` not zero,
writes its exponent `e` as two bytes, `e + 32768` high byte first, then each
digit as `digit + 1`, then 0. A digit byte is never 0, so a shorter digit run
orders first.

Escaping writes byte 0 as `0, 255`; the terminator is `0, 1`. A tuple of
values encodes as the concatenation of their keys, so a tuple orders by its
first value, then its second.

`Document` and `List` values cannot be keys.

## 5. Storage

The storage engine is a log-structured merge tree over keys and values, both
`[UN8]`.

### 5.1 Sequence numbers

Every write batch takes the next sequence number, starting at 1. A read at
sequence `s` sees, for each key, the newest version whose sequence is at most
`s`. A version is a `Put(value)` or a `Delete`.

### 5.2 Directory

A database is a directory holding:

| File | Content |
| --- | --- |
| `MANIFEST` | The current state (section 5.6) |
| `<n>.log` | A write-ahead log |
| `<n>.table` | A sorted table |

`<n>` is a file number in decimal. File numbers are never reused.

### 5.3 Write-ahead log

A log is a sequence of records. A record is:

| Field | Bytes |
| --- | --- |
| payload length | 4, high byte first |
| checksum | 4, high byte first: CRC-32C of the payload |
| payload | the batch |

A batch payload is its sequence as a variable-length number, its count, then
each operation: a kind byte (1 put, 2 delete), the key as length and bytes,
and for a put the value as length and bytes.

Each batch is written at the log's end and synced before the write returns.
Recovery replays records in order and stops at the first record that is
truncated or fails its checksum: that record and everything after it was
never acknowledged.

### 5.4 Memory table

Writes go to the log, then to the memory table: an ordered index of versions
by key ascending, then sequence descending. When it holds more than
`Tuning.memoryTableBytes`, it is written out as a sorted table, the manifest
records the table, and its log is removed.

### 5.5 Sorted tables

A sorted table holds versions in key ascending, sequence descending order.

```
data block 0 ... data block n-1
filter block
index block
footer
```

A data block holds versions until it passes `Tuning.blockBytes`. A version is
written as: key length, key bytes, sequence, kind (1 put, 2 delete), and for
a put value length and value bytes, all lengths and the sequence as
variable-length numbers.

The index block holds, per data block: the block's last key as length and
bytes, its offset and its length.

The filter block is a Bloom filter over every key in the table:
`Tuning.filterBitsPerKey` bits per key, and never fewer than 64 bits, with
`Tuning.filterProbes` probes. `h1` is the 32-bit FNV-1a hash of the key
(offset basis 2166136261, prime 16777619); `h2` continues the same hash over
the key's bytes a second time, starting from `h1`. Probe `i`, from 0, sets bit
`(h1 + i * h2) mod bits`. It is written as: bit count, probe count, then the
bits, eight per byte, low bit first.

The footer is 24 bytes: filter offset, filter length, index offset, index
length, as four-byte high-byte-first numbers, then the magic bytes
`K I R A D B 0 1`.

Every block (data, filter, index) is followed by its 4-byte CRC-32C, high
byte first, which its length does not include.

An open table keeps its filter and index in memory and reads data blocks on
demand. A store keeps up to `Tuning.cachedBlocks` data blocks its point
reads checked: block `n` of table `t` in place
`(n + 61 t) mod Tuning.cachedBlocks`, replacing what was there. A point read
of a kept block reads it from memory. A table never changes once written and
its number is never reused, so a kept block never goes stale.

### 5.6 Manifest

`MANIFEST` is text, one fact per line:

```
next-file <n>
last-sequence <s>
log <n>
table <level> <n> <smallest key hex> <largest key hex>
```

It is replaced whole: written to `MANIFEST.next`, synced, renamed over
`MANIFEST`, and the directory synced.

### 5.7 Compaction

Level 0 holds flushed tables, which may overlap. When level 0 holds
`Tuning.levelZeroTables` tables, they merge with every overlapping level 1
table into new level 1 tables. Level `n` (n >= 1) holds tables that never
overlap; when its total size passes `Tuning.levelBytes * 10^(n-1)`, its
oldest table merges into level `n + 1`. A merge keeps, per key, every version
newer than the oldest open snapshot and the newest one at or below it; a
`Delete` at the deepest level is dropped.

## 6. Documents

A collection's documents live at keys `encode(Text("d"), Text(collection),
key)`, where `key` is the document's key value (section 4). Its value is the
document's value codec (section 3.1).

### 6.1 Recorded runs

Every read and write of the document and table faces reaches the store
through three operations of `Database`: read a key, scan a key range, and
write a batch. While a run is being recorded on a database:

- a write batch is not written: its writes are held, in order, and a later
  write to the same key replaces the held one;
- a read sees the held writes over the store: a held put is the key's value
  and a held delete removes it;
- every read is recorded as a key range: a scan from `start` up to, not
  including, `end` as that range, and a read of `key` as the range from `key`
  up to, not including, `key` followed by byte 0, which holds only `key`.

Ending the run answers its recorded ranges and its held writes as one batch,
and the database reads and writes the store directly again. A recorded run
can be marked and rolled back to the mark, which drops the writes held since.

## 7. Tables

### 7.1 Schemas

```kira
enum ColumnType { Bool Number Text Bytes List Document }
struct Column { name: String, type: ColumnType, optional: Bool }
struct IndexSchema { name: String, columns: [String], unique: Bool }
struct TableSchema {
    name: String
    columns: [Column]
    primaryKey: [String]
    indexes: [IndexSchema]
}
```

A schema is valid when:

- its name is not empty and no two columns share a name;
- the primary key names at least one column, each once;
- every primary key column exists, is not `optional` and has an orderable
  type: `Bool`, `Number`, `Text` or `Bytes`;
- every index column exists and has an orderable type;
- no two indexes share a name, and each names at least one column, each once.

### 7.2 Rows

A row is a `Document`. It is valid for a schema when every field names a
column, every non-optional column has a field, and every field's value has
its column's type; an optional column's field may be absent or `Null`, which
mean the same. A stored row holds its fields in column order, and leaves out
absent and `Null` optional fields.

A row's primary key is its primary key columns' values, in primary key order.

### 7.3 Storage

The catalog lives under keys that start with `Text("c")`:

| Key | Value |
| --- | --- |
| `encode(Text("c"), Text("next-id"))` | the value codec of `Number(n)`: the next id to give |
| `encode(Text("c"), Text("table"), Text(name))` | the value codec of the table's catalog document |

A `Table` is a schema with the ids the catalog gave it; creating a table and
looking one up by name answer it:

```kira
struct Table { schema: TableSchema, id: Number, indexIds: [Number] }
```

Ids start at 1 and are never reused. Creating a table takes one id for the
table, then one for each index, in declaration order. A table's catalog
document has the fields:

| Field | Value |
| --- | --- |
| `id` | `Number(table id)` |
| `columns` | `List` of `Document`s `{name: Text, type: Text, optional: Bool}`; `type` is the case name, such as `"Number"` |
| `primaryKey` | `List` of `Text` column names |
| `indexes` | `List` of `Document`s `{name: Text, id: Number, columns: List of Text, unique: Bool}` |

A row lives at `encode(Text("t"), Number(table id), primary key values...)`
and its value is the stored row's value codec. An index entry lives at
`encode(Text("i"), Number(index id), indexed values..., primary key
values...)` with an empty value; an absent optional value is indexed as
`Null`.

### 7.4 Writes

Each write is applied as one batch: the row, its index entries and, for
creating and dropping tables, the catalog change together.

| Write | Effect |
| --- | --- |
| create table | Checks the schema, fails when the name is taken, stores the catalog entry |
| drop table | Removes every row, every index entry and the catalog entry |
| insert | Fails when a row with the same primary key exists |
| replace | Inserts, or replaces the row with the same primary key |
| update | Sets the given fields on the row with the given primary key; fails when there is none; the updated row must be valid and keep its primary key |
| delete | Removes the row with the given primary key, if there is one |

A unique index fails a write that would give two rows the same indexed values;
rows with any `Null` indexed value are not compared.

### 7.5 Queries

```kira
enum Relation { Equal NotEqual Less LessOrEqual Greater GreaterOrEqual }
struct Comparison { column: String, relation: Relation, value: Value }
enum Condition {
    Compare(Comparison)
    IsNull(String)
    AllOf([Condition])
    AnyOf([Condition])
}
enum Direction { Ascending Descending }
struct Order { column: String, direction: Direction }
struct Query {
    table: Table
    conditions: [Condition]
    orders: [Order]
    limit: UNumber?
    columns: [String]
}
```

A query answers the rows of its table for which every condition holds,
sorted by its orders, at most `limit` of them, each holding only `columns`
when `columns` is not empty.

A query is valid when every column it names exists in the table; a
`Compare` names an orderable column and a non-`Null` value of the column's
type; `IsNull` names an optional column; an `Order` names an orderable column.

Values compare in key order (section 4). A `Compare` on an absent value is
false. `AllOf([])` is true and `AnyOf([])` is false. Rows equal on every
order come in primary key order.

### 7.6 Plans

A query reads one source, then checks every condition on each row it reads:

1. **Primary key.** The top-level `Equal` comparisons fixing the leading
   primary key columns, then at most one range (`Less`, `LessOrEqual`,
   `Greater`, `GreaterOrEqual`) on the next one, bound a scan of the table's
   rows.
2. **Index.** The same, over an index's columns, bounds a scan of its
   entries; each entry's primary key reads the row.

The source is the one whose bound fixes the most columns by `Equal`, then has
a range; ties go to the primary key, then to indexes in declaration order. An
index that fixes nothing is never chosen: with no bound, the primary key scans
every row.

## 8. Sync

A sync server holds the database of record. Each client holds a replica: a
database of its own, in its own directory, that holds the parts of the
server's database the client's queries read, plus the client's mutations the
server has not yet applied. Clients never write the server's database
directly: they ask the server to run a mutation. The server runs every
mutation, one at a time, so every mutation sees every one before it.

### 8.1 Functions

The program declares its queries and mutations once, as Kira functions, and
gives the same `Functions` to its server and its clients.

```kira
struct Caller { client: Number, identity: Value }
enum FunctionFailure {
    Database(DatabaseFailure)
    Table(TableFailure)
    Refused(String)
    NoSuchFunction(String)
}
struct QueryFunction {
    name: String
    run: (borrow Database, borrow Caller, borrow Value) -> Result<Value, FunctionFailure>
}
struct MutationFunction {
    name: String
    run: (borrow mut Database, borrow Caller, borrow Value) -> Result<Value, FunctionFailure>
}
struct Functions { queries: [QueryFunction], mutations: [MutationFunction] }
```

A function reads and writes only through the database's document and table
faces, and answers the same for the same database, caller and arguments. A
query never writes. Names are unique within `queries` and within
`mutations`.

Every function runs as a recorded run (section 6.1). A mutation that answers
`Ok` has its held writes applied; one that answers `Error` has none applied.

### 8.2 Sync keys

The server and every replica keep their sync state under keys that start
with `Text("s")`, which no function reaches:

| Key | Where | Value |
| --- | --- | --- |
| `encode(Text("s"), Text("next-client"))` | server | `Number(n)`: the next client id to give, from 1 |
| `encode(Text("s"), Text("applied"), Number(client))` | server | `Number(n)`: the client's last applied mutation number |
| `encode(Text("s"), Text("client"))` | replica | `Number(id)`: the id the server gave |
| `encode(Text("s"), Text("identity"))` | replica | the identity the last `welcome` gave |
| `encode(Text("s"), Text("version"))` | replica | `Number(v)`: the server version the replica holds |
| `encode(Text("s"), Text("next-mutation"))` | replica | `Number(n)`: the next mutation number, from 1 |
| `encode(Text("s"), Text("pending"), Number(n))` | replica | `{name: Text, arguments: Value}`: mutation `n`, not yet applied |
| `encode(Text("s"), Text("held"))` | replica | `List` of `{start: Bytes, end: Bytes}`: the ranges the replica holds |

The server's version is its store's last sequence (section 5.1).

### 8.3 Messages

A client sends:

| Message | Fields | Meaning |
| --- | --- | --- |
| `hello` | `client: Number`, `credentials: Value` | Opens the session; `client` is 0 for a client the server never named |
| `subscribe` | `query: Number`, `name: Text`, `arguments: Value` | Keeps the replica holding what the query reads |
| `unsubscribe` | `query: Number` | Stops it |
| `mutate` | `number: Number`, `name: Text`, `arguments: Value` | Asks the server to run a mutation |

The server sends:

| Message | Fields | Meaning |
| --- | --- | --- |
| `welcome` | `client: Number`, `identity: Value` | The session is open |
| `refused` | `reason: Text` | The session ends |
| `transition` | `version: Number`, `applied: Number`, `changes: List`, `ready: List`, `results: List` | The replica's next state |

A message is a `Document` whose first field is `type: Text(name)`, followed by
the fields above in order. On a byte stream, each message is a frame: the
length of its value codec (section 3.1) as a variable-length number (section
3.2), then the value codec.

A `transition`'s `changes` are documents, applied in order:

| Change | Fields | Effect on the replica |
| --- | --- | --- |
| `write` | `key: Bytes`, `value: Bytes` or `Null` | Puts the value at the key, or deletes the key when `Null` |
| `hold` | `start: Bytes`, `end: Bytes`, `pairs: List` of `{key: Bytes, value: Bytes}` | The range now holds exactly the pairs |
| `release` | `start: Bytes`, `end: Bytes` | The range is no longer held: its keys are removed |

Its `ready` lists the subscriptions whose reads the replica holds for the
first time. Its `results` are documents `{number: Number}` followed by one of
`value: Value` (the mutation answered it), `failure: Text` (the mutation
failed, and wrote nothing) or `skipped: true` (it was applied before, in an
earlier session). Its `applied` is the client's last applied mutation number.

### 8.4 The server

The server runs `hello` through its `authenticate` function, which answers
the caller's identity for the credentials, or a reason to refuse. A refused
session gets `refused` and ends. An accepted one is named: a `client` of 0
takes the next client id; any other is kept. The server answers `welcome`.

A session holds a set of key ranges: the union of the ranges its
subscriptions' last runs recorded. A `subscribe` runs the query at the
current version as the caller and keeps its recorded ranges, also when it
fails. An `unsubscribe` drops the subscription.

A `mutate` numbered `n` for a client whose last applied number is `a`:

- with `n <= a`, is answered `skipped`;
- with `n == a + 1`, runs. Its held writes, when it answers `Ok`, and the
  client's applied number `n` are written as one batch. An unknown name
  fails as `NoSuchFunction`;
- with `n > a + 1`, refuses the session: `mutation out of order`.

After every batch, every subscription whose ranges hold a key the batch
wrote runs again. Then every session whose held ranges, writes or results
changed is sent a `transition` at the new version. For a session whose held
set went from `old` to `new`:

1. `write` for each write of the batch to a key in both `old` and `new`, in
   batch order;
2. `hold` for each range of `new` outside `old`, with its pairs at the new
   version;
3. `release` for each range of `old` outside `new`.

Ranges are kept as a sorted list of disjoint, non-touching ranges; the
ranges of a difference are its maximal ranges.

Any message the server cannot read, an unknown `type`, and any message before
`hello` refuses the session: `malformed message`.

### 8.5 The replica

`openReplica(directory, functions)` opens or creates the replica's database.
The client's view is the replica with its pending mutations run over it, in
number order, as one recorded run that is never ended; a pending mutation
that fails holds no writes. A query's result is its run over the view.

- `subscribe(name, arguments)` gives the query the next query id, from 1.
- `mutate(name, arguments)` takes the next mutation number, stores the
  pending mutation, and runs it over the view.
- `connect(credentials)` starts a session: it sends `hello`, then
  `subscribe` for every subscription and `mutate` for every pending
  mutation, in number order.
- `disconnect()` ends it.

While a session is open, `subscribe`, `unsubscribe` and `mutate` also send
their messages.

When a session opens, the replica's held set becomes its stale set and its
held set is empty: the server holds nothing for a new session. A `hold` adds
its range to the held set and a `release` removes its range.

On `welcome`, a replica that had no client id stores the one given, and it
stores the identity. The view runs as the caller of the stored client id and
identity: 0 and `Null` before the first `welcome`. On a
`transition`, the replica writes in one batch: its changes, the version, the
union of its held and stale sets as `held`, and the removal of every pending
mutation numbered up to `applied`. Once every subscription is ready, the
same batch also removes the keys of the stale ranges outside the held set,
and the stale set becomes empty. Then the replica runs its remaining pending
mutations again to rebuild the view.

A replica's subscriptions last for one open replica; queries are subscribed
again when it is opened again.

The replica reports events: `Welcomed(identity)`, `Refused(reason)`,
`Changed(query)` when a query's result over the view differs from the one
last reported, and `Done(number, outcome)` when a mutation's result arrives.

### 8.6 Transport

A session is one bidirectional stream carrying frames (section 8.3) both
ways. Over the network it is a QUIC connection of Foundation's `QuicNode`,
opened with protocol `kiradb`, whose first bidirectional stream, opened by
the client, carries the session. The engines of sections 8.4 and 8.5 take
messages in and give messages out, and never own a stream; the bindings
below carry their frames. KiraDB depends on Foundation alone.

```kira
public struct SyncHost {
    var server: SyncServer
    function event(borrow mut self, node: borrow mut QuicNode, event: borrow QuicNodeEvent) -> Bool
}
public function hostSync(server: SyncServer, node: borrow mut QuicNode, address: borrow String, identity: QuicIdentity) -> Result<SyncHost, QuicFailure>

public struct SyncLink {
    var replica: Replica
    function start(borrow mut self, node: borrow mut QuicNode, dial: QuicDial, credentials: Value) -> Result<Void, QuicFailure>
    function event(borrow mut self, node: borrow mut QuicNode, event: borrow QuicNodeEvent) -> Bool
    function send(borrow mut self, node: borrow mut QuicNode)
    function connected(borrow self) -> Bool
}
public function linkSync(replica: Replica) -> SyncLink
```

The program owns the node and runs its `poll` or `wait` loop, handing every
event to each binding, which answers whether the event was its own.

`hostSync` listens on `address` with `identity`. The host:

- takes a connection its listener accepted, and opens a server session for
  the first bidirectional stream the client opens on it; the connection's
  other streams are stopped with code 0;
- feeds that stream's bytes through a `FrameReader` to the server session;
- after every event it takes, writes each outgoing message as a frame on
  its session's stream. A server session that has ended gets its stream
  finished and is closed;
- closes the server session when its stream ends or its connection closes;
- when the stream's bytes are not frames, or the server fails, closes the
  server session and the connection with code 1 and the failure's text.

The link:

- `start` dials. When the connection opens, the link opens one
  bidirectional stream and connects the replica with `credentials`;
- feeds the stream's bytes through a `FrameReader` to the replica;
- `send` writes each message the replica gives as a frame on the stream,
  and drops them while no stream is open: `connect` sends again what a new
  session needs. The link sends after every event it takes; the program
  calls `send` after `subscribe`, `unsubscribe` or `mutate`;
- disconnects the replica when the stream ends or the connection closes,
  and closes the connection with code 0 when the stream ends;
- when the stream's bytes are not frames, or the replica fails on one,
  disconnects the replica and closes the connection with code 1 and the
  failure's text.

`connected()` answers whether the link's stream is open. A program
reconnects by calling `start` again; a `start` while connected first closes
the connection it had with code 0.

## 9. Replication

A replication group is a fixed set of members, each a server with its own
directory, that together hold one database of record. One member, the
leader, runs every mutation; the others follow its log. A write is
acknowledged once a majority of members hold it, so the group keeps every
acknowledged write and keeps answering while a majority is up. When the
leader stops, the others elect another.

Sync (section 8) runs on top: its server is the group's leader, and a
mutation that reaches any other member is forwarded to it.

### 9.1 Members

```kira
struct Member { id: String, address: String }
struct Group { members: [Member], me: String }
```

Ids are unique and never reused. `me` names this member. A group of one
member is its own leader and acknowledges writes at once. A majority is
`floor(count / 2) + 1` members.

### 9.2 The log

Every change to the database of record is an entry of the group's log:

| Field | Value |
| --- | --- |
| `index` | `Number`: its place in the log, from 1 |
| `term` | `Number`: the term of the leader that wrote it |
| `batch` | `Bytes`: a batch payload (section 5.3) with sequence 0 |
| `notes` | `List`: values the application attaches; KiraDB does not read them |
| `members` | `List` of `{id: Text, address: Text}`, or absent: the group's new member list (section 9.8) |

An entry with an empty batch is allowed: a leader writes one when elected
(section 9.4), and an application may write one to carry notes alone.

A member applies entries in index order, each as one write batch to its
database of record: the entry's writes, followed by a put of
`Number(index)` at `encode(Text("r"), Text("applied"))`. So the database
always says which entry it reflects, and a crash never applies an entry
twice. After applying, the member hands the entry's notes to the
application.

### 9.3 State

Each member keeps its replication state in a second database, its log
database, in the directory `replication` inside its own:

| Key | Value |
| --- | --- |
| `encode(Text("r"), Text("term"))` | `Number(t)`: the latest term the member has seen |
| `encode(Text("r"), Text("vote"))` | `Text(id)`: whom it voted for in that term, or `Null` |
| `encode(Text("r"), Text("snapshot"))` | `{index: Number, term: Number, members: List}`: the last entry the log no longer holds, and the member list as of it |
| `encode(Text("r"), Text("log"), Number(index))` | `{term: Number, batch: Bytes, notes: List}` |

The term and vote are written, and synced, before any message that
depends on them is sent. An entry is written before the member answers
that it holds it.

The first member of `members` starts the group. On its first start, with
no log database, it takes snapshot `{index: 1, term: 1}`, term 1 and
applied index 1, whatever its database of record holds: its contents are
entry 1, already compacted. Every other member starts at 0 and does not
stand for election until it holds an entry. A group's existing data
therefore belongs on its first member.

### 9.4 Roles

A member is a follower, a candidate or the leader of a term. Every member
starts a follower. Timing is `Tuning.heartbeat` (250 ms) and
`Tuning.election` (1500 ms).

- A follower that holds at least one entry and hears nothing from a leader
  or candidate for a random time between `election` and twice `election`
  becomes a candidate.
- A candidate takes the next term, votes for itself, and sends `vote` to
  every member. With a majority of votes it becomes leader. It returns
  to follower when it sees a leader of its term or a later term.
- A leader sends `append` to every member at least every `heartbeat`, and
  as soon as it has entries a member lacks. On being elected it writes an
  entry with an empty batch, so that entries of earlier terms commit.

Any message carrying a later term than the member's makes it a follower
of that term, its vote cleared, before it handles the message.

A member grants a `vote` when the request's term is at least its own,
it has not voted for another member in that term, and the candidate's log
is at least as up to date as its own: a later last term, or the same last
term and a last index at least as great.

### 9.5 Appending

`append` carries the leader's term and id, the index and term of the entry
before the new ones (`previous`), the new entries, and the leader's commit
index. A follower:

- refuses an `append` of an earlier term;
- refuses when it does not hold an entry at `previous` with that term,
  answering its last index so the leader can step back;
- otherwise removes every entry from the first one whose term differs from
  the new entry at the same index, appends the entries it lacks, and
  answers the index of the last entry it now holds from this leader.

Its commit index becomes the smaller of the leader's and its last new
entry's index. `live` lists the members the leader heard from within
`election`, itself included; members use it to share work by who is up.

The leader keeps, for each member, the next index to send and the highest
index the member holds. An entry is committed when a majority of members
hold it and it belongs to the leader's term; every entry before a
committed entry is committed too. Each member applies committed entries
(section 9.2) in order.

### 9.6 Mutations

Only the leader writes the log. A mutation runs on the leader, against its
database of record, one at a time (as in section 8.4). Its writes become one
entry, and the mutation answers once that entry is applied on the leader.
A member that is not the leader forwards a mutation to the leader it knows,
and answers once it has applied the leader's entry itself, so a caller
always reads its own writes from the member it asked. With no leader known,
the mutation fails as `Unavailable`; the caller may retry.

### 9.7 Snapshots

A member may drop applied entries from its log; the snapshot record says
up to where. A leader that would send a member an entry it no longer
holds sends `install` instead: every key and value of its database of
record at its applied index `i` and that entry's term `t`, in key order,
in pieces. The leader reads every piece as of one sequence (section 5.1), so
the copy is consistent without the leader holding all of it at once. The
follower writes the pieces into a new database of record,
and when the last arrives it replaces its database of record with it,
records snapshot `{index: i, term: t}`, and drops every entry up to `i`.

### 9.8 Changing members

The member list changes one member at a time, through the log. An entry with
`members` replaces the list, and every member uses the newest list its log
holds (from its last such entry, or its snapshot, or `Group.members`) from
the moment it holds the entry, committed or not: for majorities, elections
and whom to send to. When such an entry is removed from a log, the member
returns to the list before it.

- The leader writes a change only when every earlier change is committed,
  and only one that adds or removes a single member.
- An added member starts empty, with `members` naming the group it joins,
  and catches up by `install` and `append` like any member that fell
  behind. It stands for election only once it holds an entry.
- A removed member stops receiving messages. A leader that removed itself
  steps down once the change commits. A member not in the list never
  stands for election.

### 9.9 Messages

Members send each other:

| Message | Fields | Answer |
| --- | --- | --- |
| `vote` | `term`, `candidate: Text`, `lastIndex`, `lastTerm` | `voted`: `term`, `granted: Bool` |
| `append` | `term`, `leader: Text`, `previousIndex`, `previousTerm`, `entries: List`, `commit`, `live: List` | `appended`: `term`, `success: Bool`, `last: Number` |
| `install` | `term`, `leader`, `index`, `indexTerm`, `members: List`, `offset`, `pairs: List` of `{key: Bytes, value: Bytes}`, `done: Bool` | `installed`: `term` |
| `forward` | `batch: Bytes`, `notes: List` | `forwarded`: `index: Number`, or `failure: Text` |

Messages are documents, framed as in section 8.3. In Kira they travel over
a QUIC connection of protocol `kiradb-replication` between members; a
member authenticates another by a secret the group shares. Other
implementations may carry the same documents over another transport, as
long as each message reaches its member whole and its answer comes back
to the sender.
