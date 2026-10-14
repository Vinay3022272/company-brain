# Company Brain (FalkorDB + LLM Wiki): Validation and End-to-End Flow

Track 3: Company Brain | Graph Hacks Hackathon

---

## 0. TL;DR verdict

**The draft is a good fit for Track 3 and worth building.** It covers every Track 3 focus bullet (docs/tickets/chats in one graph, who owns/knows what, decision tracing, freshness, sourced answers). The wiki layer is a genuine differentiator.

It has **six gaps** that could cost you with judges. All are fixable without adding much scope:

| # | Gap in the draft | Fix in this doc |
|---|---|---|
| 1 | The wiki can look like a "second database beside the graph", which violates "the graph has to shape what the agent does" | Wiki pages are *generated views of graph fact packs*, tracked as `WikiPage` nodes with `SUMMARIZES` edges and a `fact_hash`. The agent's claims cite graph evidence, not wiki prose (§9) |
| 2 | The agent only does multi-hop lookups. No graph algorithms | Add algorithm tools: expert scoring, PageRank on dependencies, betweenness/bus-factor, shortest path (§8, §10) |
| 3 | "Who knows what" is only `OWNS`/`FIXED`, so it is just lookup | Inferred, weighted `KNOWS_ABOUT` edges with a score breakdown the agent can explain (§8) |
| 4 | No entity resolution ("Priya", "P. Sharma", "priya@…") | Deterministic-first resolver using FalkorDB full-text index + graph-neighbourhood LLM adjudication (§6) |
| 5 | No time model or supersession. Decisions get replaced, owners change | `valid_from/valid_to/status` on edges, `SUPERSEDES` between decisions, `Conflict` nodes (§4, §7) |
| 6 | "Provenance" is stated but not enforced | `Evidence` nodes with verbatim quotes that are *verified as substrings* of the source, plus an answer verifier that rejects uncited claims (§6, §10) |

Also add: **a small eval set with a vector-RAG baseline** (§14). It turns "looks cool" into "measurably better", which is what makes the demo convincing.

---

## 1. Validation of the draft

### 1.1 Fit against the brief

| Brief requirement | Covered? | Where |
|---|---|---|
| Follow connections across data | Yes | Multi-hop tools, `find_paths` |
| Remember across sessions | Partly | The graph persists; add optional `Query`/feedback nodes (§19) |
| Share state between agents | Optional | Ingest agent and Answer agent share the graph; mention it, don't over-build |
| Connect people, projects, decisions | Yes | Core schema |
| Show the path behind an answer | Yes, if you build the trace store (§10) | Highlighted path + evidence drawer |
| Track 3: knowledge from docs/tickets/chats in one graph | Yes | Ingestion over 4 source kinds |
| Track 3: who owns / who knows | Yes after Gap 3 | `OWNS` + inferred `KNOWS_ABOUT` |
| Track 3: trace decisions to source | Yes | `trace_decision` tool |
| Track 3: keep knowledge current | Yes, and it's your best demo moment | Update pipeline + dirty-page regen (§7, §13) |
| Track 3: answers with clear sources | Yes | Citation verifier |

### 1.2 What's strong

- Clear separation of roles (graph = relationships, wiki = readable narrative, sources = evidence).
- Provenance and conflict-marking are already in the design; most teams skip them.
- Realistic MVP scope and a sensible no-vector-DB stance.

### 1.3 Weak spots and how this doc resolves them

1. **Wiki vs. graph drift.** Rule: *the wiki is never an input to extraction and never the source of a claim.* It is regenerated when its fact pack hash changes. The agent reads it only for narrative context.
2. **"Why not just RAG?"** Judges will ask. Answer with (a) questions that *require* 3+ hops or an algorithm and (b) the eval table vs a chunk-search baseline.
3. **LLM-everything fragility.** Parse structured sources (org CSV, ticket JSON) deterministically. Use the LLM only for prose (docs, meeting notes, chats).
4. **Hallucinated provenance.** Every fact carries a verbatim quote; the quote must appear in the source chunk or the fact is dropped.
5. **Scope.** Cut list in §1.4.

### 1.4 Scope discipline

**Must have:** 4 source kinds, ~7 node types, graph + wiki + chat, path highlight, evidence drawer, one live-update demo, one conflict, one supersession, eval table.

**Cut / stretch:** auth, real Slack/Jira connectors, vector search, multi-tenant UI, LangGraph, fine-tuning, a full React app (a single static page with Cytoscape.js is enough).

### 1.5 Things to verify yourself

- Official rules and judging criteria on the hackathon page. I only saw your pasted summary. In particular confirm: the code-start date (planning before kickoff is allowed, but main coding must start after), the required README contents, demo video length, and any LLM/tool restrictions.
- Exact FalkorDB syntax for your installed version (full-text index DDL, `algo.*` procedure names/yield fields). The signatures below match the docs as I know them, but confirm at docs.falkordb.com/cypher and docs.falkordb.com/algorithms before you rely on them.

---

## 2. Product definition

**One-liner:** A Company Brain that turns scattered docs, tickets, chats and meeting notes into a FalkorDB knowledge graph plus an always-current wiki, and answers questions with the evidence and the graph path behind every claim.

**Users:** a new engineer, an on-call responder, a manager asking "why did we decide this?".

**Five hero questions (the demo and the eval are built around these):**

| Type | Example question | Graph work required |
|---|---|---|
| Expert finder | "Who can help fix the payment timeout issue, and why them?" | Topic to tickets to fixers, recency decay, current team, similar tickets |
| Decision trace | "Why do we use Vendor B's API for payments?" | `SUPERSEDES` chain, rationale, decider, source doc |
| Ownership + conflict | "Who owns the Payment Gateway?" | Active ownership, disputed edge, `Conflict` node |
| Impact analysis | "If Auth Service goes down, what breaks and who do we call?" | Variable-length `DEPENDS_ON`, owners |
| Knowledge risk | "Which systems have a bus-factor problem?" | Betweenness/`KNOWS_ABOUT` counts |

Plus the **living knowledge** scenario: ingest one new document and watch graph edges, wiki pages and an answer change.

---

## 3. Architecture

### 3.1 Diagram

```
                ┌──────────────────────── SOURCES ────────────────────────┐
                │ docs/ADRs   tickets.json   chat logs   meeting notes    │
                │ org.csv                                                 │
                └───────────────┬─────────────────────────────────────────┘
                                │
                  ┌─────────────▼──────────────┐
                  │ 1. INGEST                  │  hash, register Document,
                  │ loader · chunker · registry│  chunk by section/message
                  └─────────────┬──────────────┘
                                │ chunks
          ┌─────────────────────▼──────────────────────┐
          │ 2. EXTRACT                                 │
          │ structured parsers (CSV/JSON) ─┐           │
          │ LLM extractor (prose) ─────────┼─► facts   │
          │ quote verifier · type checker ─┘           │
          │ entity resolver (FT index + LLM adjudicate)│
          └─────────────────────┬──────────────────────┘
                                │ verified facts + evidence
          ┌─────────────────────▼──────────────────────┐
          │ 3. GRAPH WRITER  (idempotent MERGE)        │
          │ supersession · conflict detection          │
          └─────────────────────┬──────────────────────┘
                                ▼
   ┌───────────────────────────────────────────────────────────┐
   │                    FALKORDB (the brain)                   │
   │ entities · relationships · Evidence · Document            │
   │ Conflict · WikiPage · (optional) Query log                │
   │ graph algorithms: PageRank · betweenness · WCC · paths    │
   └───────┬──────────────────────┬────────────────────┬───────┘
           │                      │                    │
 ┌─────────▼────────┐  ┌──────────▼─────────┐  ┌───────▼──────────────┐
 │ 4. ANALYTICS     │  │ 5. WIKI GENERATOR  │  │ 6. COMPANY BRAIN     │
 │ inferred edges:  │  │ fact pack (Cypher) │  │ AGENT                │
 │ KNOWS_ABOUT,     │  │ → template + LLM   │  │ tools = Cypher +     │
 │ SIMILAR_TO       │  │ prose → .md        │  │ algorithms (MCP)     │
 │                  │  │ fact_hash → stale? │  │ trace + verifier     │
 └──────────────────┘  └──────────┬─────────┘  └───────┬──────────────┘
                                  │ markdown           │ answer + claims
                                  ▼                    ▼
                         ┌─────────────────────────────────────┐
                         │ 7. API (FastAPI) + UI               │
                         │ chat · graph path · evidence · wiki │
                         │ conflicts · ingest + diff view      │
                         └─────────────────────────────────────┘
```

### 3.2 Source-of-truth rules (put these in your README)

1. **Sources** (documents, tickets, chats) are the ground truth of *what was said*.
2. **FalkorDB** is the truth of *what is connected to what*, and every edge points back to evidence.
3. **The wiki** is a *derived view*. It is regenerated from graph fact packs, never edited as truth, never fed back into extraction.
4. **The agent** may only assert what a tool returned in the current run, and it must cite the evidence ids.

### 3.3 Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Graph | FalkorDB (Docker) | `falkordb` Python client; `ro_query` for agent safety |
| Backend | Python 3.11 + FastAPI | |
| LLM | Any model with tool calling + JSON output | Cheaper model for extraction, stronger one for the agent loop |
| Schemas | Pydantic v2 | Validate every LLM output |
| Agent | Custom loop (about 100 lines) | Easier to debug and trace than a framework. LangGraph is optional |
| MCP | `mcp` Python SDK (`FastMCP`) | Expose the same tools over MCP, a cheap bonus |
| Wiki | Markdown files + `WikiPage` nodes | |
| UI | Single static page + Cytoscape.js served by FastAPI (team) or Streamlit + pyvis (solo) | Path highlighting is the must-have |
| Retrieval | FalkorDB full-text index | Used only for entity lookup and evidence search. No separate vector DB |

Sponsor-listed options (GraphRAG-SDK, Graphiti, Cognee) can save time but hide schema control. Use them only if extraction becomes your bottleneck.

### 3.4 Run it

```yaml
# docker-compose.yml
services:
  falkordb:
    image: falkordb/falkordb:latest
    ports: ["6379:6379", "3000:3000"]   # 3000 = FalkorDB Browser
    volumes: ["falkor_data:/var/lib/falkordb/data"]
volumes: { falkor_data: {} }
```

---

## 4. Graph data model

### 4.1 Nodes

All entity nodes share: `id` (stable, prefixed, e.g. `p_priya`), `name`, `created_at`, `updated_at`.

| Label | Key properties |
|---|---|
| `Person` | `email`, `title`, `aliases[]`, `active` |
| `Team` | `aliases[]` |
| `Project` | `status`, `summary` |
| `System` | `kind` (service/db/library), `aliases[]` |
| `Ticket` | `key` (BUG-123), `title`, `status`, `severity`, `opened_at`, `resolved_at` |
| `Incident` | `title`, `started_at`, `resolved_at`, `severity` |
| `Decision` | `title`, `rationale`, `decided_at`, `status` (active/superseded) |
| `Topic` | `name` (timeouts, retries, idempotency). Normalised tags, the bridge for "similar issues" |
| `Document` | `uri`, `title`, `kind` (adr/postmortem/meeting/chat/ticket/org), `date`, `authority` (1-5), `sha256` |
| `Evidence` | `doc_id`, `quote`, `chunk_id`, `char_start`, `char_end` |
| `Conflict` | `type`, `status` (open/resolved), `detected_at`, `note` |
| `WikiPage` | `path`, `entity_id`, `fact_hash`, `generated_at`, `status` (fresh/stale) |

FalkorDB properties are scalars or arrays of scalars (no nested maps), so keep structure flat and use nodes for anything richer.

### 4.2 Relationships

| Edge | From → To | Notes |
|---|---|---|
| `MEMBER_OF` | Person → Team | Functional (one active team) |
| `OWNS` | Team/Person → Project/System | Functional per target |
| `WORKED_ON` | Person → Project | |
| `ASSIGNED_TO` | Person → Ticket | |
| `FIXED` | Person → Ticket | |
| `REPORTED` | Person → Ticket | |
| `HAS_TICKET` | Project → Ticket | |
| `ABOUT` | Ticket/Incident/Decision/Document → Topic | Enables similar-issue hops |
| `AFFECTS` | Ticket/Incident/Decision → System/Project | |
| `DEPENDS_ON` | System → System | Basis for PageRank/impact |
| `MADE_BY` | Decision → Person | |
| `RECORDED_IN` | Decision/Ticket/Incident → Document | |
| `SUPERSEDES` | Decision → Decision | |
| `AUTHORED` | Person → Document | |
| `RESOLVED_BY` | Incident → Ticket/Decision | |
| `KNOWS_ABOUT` | Person → Topic/System | **Inferred**, `score`, `breakdown` |
| `SIMILAR_TO` | Ticket → Ticket | **Inferred**, shared topics/system |
| `MENTIONS` | Evidence → any entity | Discovery from evidence to entities |
| `FROM` | Evidence → Document | |
| `INVOLVES` | Conflict → any entity | |
| `SUMMARIZES` | WikiPage → entity | |
| `LINKS_TO` | WikiPage → WikiPage | Wiki navigation |

### 4.3 Provenance on every fact edge

Every fact edge carries:

```
status        'active' | 'superseded' | 'disputed'
valid_from    ISO date (date of the *source*, not ingest time)
valid_to      ISO date or null
confidence    0..1
evidence_ids  [ 'ev_ab12', ... ]      // arrays are allowed
source        'structured' | 'llm' | 'inferred'
```

Evidence lookup: `MATCH (ev:Evidence) WHERE ev.id IN r.evidence_ids`. This keeps the graph light while retaining the full quote and source link.

### 4.4 Indexes

```cypher
CREATE INDEX FOR (n:Person)   ON (n.id)
CREATE INDEX FOR (n:Team)     ON (n.id)
CREATE INDEX FOR (n:Project)  ON (n.id)
CREATE INDEX FOR (n:System)   ON (n.id)
CREATE INDEX FOR (n:Ticket)   ON (n.id)
CREATE INDEX FOR (n:Ticket)   ON (n.key)
CREATE INDEX FOR (n:Decision) ON (n.id)
CREATE INDEX FOR (n:Topic)    ON (n.id)
CREATE INDEX FOR (n:Document) ON (n.id)
CREATE INDEX FOR (n:Evidence) ON (n.id)
CREATE INDEX FOR (n:WikiPage) ON (n.id)
```

Full-text (for entity resolution and evidence search). Check the DDL in your version's docs:

```cypher
CALL db.idx.fulltext.createNodeIndex('Person', 'name', 'aliases')
CALL db.idx.fulltext.createNodeIndex('System', 'name', 'aliases')
CALL db.idx.fulltext.createNodeIndex('Ticket', 'title', 'key')
CALL db.idx.fulltext.createNodeIndex('Evidence', 'quote')
```

### 4.5 Multi-tenancy (cheap to claim, cheap to build)

One graph per company: `db.select_graph(f"brain_{tenant_id}")`. Everything else is unchanged. Mention in the README; demo with two tenants only if you have time.

---

## 5. Synthetic dataset (design this first)

The dataset *is* your demo. Design it so each hero question has a satisfying, non-trivial answer. Use a fictional company ("Acme Fintech") and mark everything synthetic.

**Size:** ~12 people, 4 teams, 5 projects, 6 systems, 25 tickets, 8 decisions, 4 incidents, ~15 documents (ADRs, postmortems, meeting notes, chat threads), 1 org CSV, 1 ticket export.

**Plant these on purpose:**

| Plant | Purpose |
|---|---|
| `BUG-123` "payment timeouts", fixed by **Priya**, documented in postmortem `INC-14` | Hero expert question |
| Priya **moved** from Payments to Platform (org CSV vs old meeting notes) | Temporal edge. Answer must say "she's now on Platform; current Payments owner is X" |
| `BUG-201` similar timeout in **Auth Service**, fixed by **Rahul** | Multi-hop via `Topic: timeouts`, `SIMILAR_TO` |
| `ADR-7` (use Vendor A API) superseded by `ADR-12` (Vendor B), different decider | Decision trace chain |
| Doc A says Gateway owned by **Payments**; later meeting notes say **Platform** took over | `Conflict` or supersession, depending on source authority |
| Auth Service is depended on by 4 systems | PageRank/impact demo |
| One system known by only one person | Bus-factor demo |
| Alias variants: "Priya S.", "psharma", "priya@acme.test" | Entity resolution |
| **Held-out update docs** in `data/updates/` | Live-update demo (e.g. "Oct 20 meeting: Priya owns retry policy; ADR-15 sets timeout to 8s") |

Write a `GROUND_TRUTH.yaml` as you author the data: expected answers, expected evidence ids and expected path for each eval question. This doubles as your test suite.

---

## 6. Pipeline A: ingestion and extraction

### 6.1 Ingest

1. Read file; compute `sha256`. If a `Document` with that hash exists, skip (idempotent).
2. Create `Document {id, uri, title, kind, date, authority, sha256}`. Authority guide: org CSV 5, ADR/official doc 4, postmortem 4, ticket 3, meeting notes 2, chat 1.
3. Chunk: ADRs/docs by heading, chats by thread, meeting notes by agenda item, tickets as one chunk. Target ~300-500 tokens. Store `chunk_id` and char offsets.

### 6.2 Extraction, two lanes

**Lane 1: deterministic (no LLM).** `org.csv` gives `Person`, `Team`, `MEMBER_OF`. `tickets.json` gives `Ticket`, `ASSIGNED_TO`, `FIXED`, `REPORTED`, `HAS_TICKET`, and the `Topic` labels. Write straight to the graph with `source='structured'`.

**Lane 2: LLM extraction for prose** (docs, postmortems, meetings, chats).

Schema (Pydantic):

```python
from typing import Literal, Optional
from pydantic import BaseModel, Field

EntityType = Literal["Person","Team","Project","System","Ticket","Incident","Decision","Topic"]
RelType = Literal["MEMBER_OF","OWNS","WORKED_ON","FIXED","ASSIGNED_TO","AFFECTS","DEPENDS_ON",
                  "MADE_BY","SUPERSEDES","AUTHORED","RESOLVED_BY","ABOUT","HAS_TICKET"]

class XEntity(BaseModel):
    local_id: str                      # e.g. "e1", unique within this chunk
    type: EntityType
    name: str
    aliases: list[str] = []
    attrs: dict[str, str | int | float | None] = {}   # e.g. rationale, decided_at
    quote: str                         # verbatim supporting text

class XRelation(BaseModel):
    src: str                           # local_id
    type: RelType
    dst: str                           # local_id
    quote: str                         # verbatim
    confidence: float = Field(ge=0, le=1)
    valid_from: Optional[str] = None   # ISO date if stated, else None (use doc date)

class Extraction(BaseModel):
    entities: list[XEntity]
    relations: list[XRelation]
```

Prompt essentials: "Extract only what the text states. Every entity and relation needs a verbatim `quote` copied from the text. Do not infer. Use only these types. If unsure, omit."

### 6.3 Verification gates (this is what makes provenance trustworthy)

```python
import re
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()

DOMAIN = {  # relationship -> allowed (src_label, dst_label)
  "MEMBER_OF": {("Person","Team")},
  "OWNS": {("Team","Project"),("Team","System"),("Person","Project"),("Person","System")},
  "FIXED": {("Person","Ticket")},
  "SUPERSEDES": {("Decision","Decision")},
  "MADE_BY": {("Decision","Person")},
  "DEPENDS_ON": {("System","System")},
  # ... complete for all RelType
}

def verify(extraction, chunk_text):
    ok_entities = {e.local_id: e for e in extraction.entities if norm(e.quote) in norm(chunk_text)}
    ok_rels = []
    for r in extraction.relations:
        s, d = ok_entities.get(r.src), ok_entities.get(r.dst)
        if not (s and d): continue
        if norm(r.quote) not in norm(chunk_text): continue
        if (s.type, d.type) not in DOMAIN[r.type]: continue
        if r.confidence < 0.6: continue
        ok_rels.append(r)
    return ok_entities, ok_rels
```

Rejected facts go to a `rejected_facts.jsonl` log. Show the count in the UI ("N facts rejected: quote not found"). Judges love seeing that guardrail.

### 6.4 Entity resolution

Goal: map extracted mentions to canonical node ids.

1. **Exact/normalised**: lowercase, strip punctuation, ticket-key regex `[A-Z]+-\d+`, email local-part, alias array.
2. **Full-text candidates**: `CALL db.idx.fulltext.queryNodes('Person', $name)` gives top-5 candidates.
3. **Ambiguous? Graph-aware LLM adjudication.** Give the LLM the mention plus its chunk context, plus each candidate's neighbourhood (team, projects, recent tickets). The graph neighbourhood is a stronger signal than names alone, and it is a nice point for "the graph shapes the pipeline". Output: `{match_id | "new", reason}`.
4. **Create-new rule**: only if no candidate passes. Never merge on a name alone when two candidates tie. Create a `Conflict` or leave as new and flag.
5. Append the new surface form to `aliases[]`.

### 6.5 Evidence creation

For each verified fact create `Evidence {id: sha(doc_id+quote), doc_id, chunk_id, quote, char_start, char_end}` plus `(ev)-[:FROM]->(doc)` and `(ev)-[:MENTIONS]->(entity)`. The fact edge stores `evidence_ids`.

---

## 7. Pipeline B: graph writer and update semantics

### 7.1 Idempotent upserts

Labels and relationship types can't be query parameters, so interpolate them from a **whitelist** and parameterise everything else.

```python
from falkordb import FalkorDB
db = FalkorDB(host="localhost", port=6379)
g = db.select_graph("acme")

LABELS = {"Person","Team","Project","System","Ticket","Incident","Decision","Topic","Document"}
RELS = {...}  # same set as RelType + inferred ones

def upsert_entity(label, id, name, attrs, now):
    assert label in LABELS
    g.query(f"""
      MERGE (n:{label} {{id:$id}})
      ON CREATE SET n.created_at=$now
      SET n.name=$name, n.updated_at=$now, n += $attrs
    """, {"id": id, "name": name, "attrs": attrs, "now": now})

def upsert_fact(src_label, src, rel, dst_label, dst, ev_id, conf, valid_from, source):
    assert src_label in LABELS and dst_label in LABELS and rel in RELS
    g.query(f"""
      MATCH (a:{src_label} {{id:$src}}), (b:{dst_label} {{id:$dst}})
      MERGE (a)-[r:{rel}]->(b)
      ON CREATE SET r.status='active', r.valid_from=$vf, r.confidence=$conf,
                    r.source=$source, r.evidence_ids=[$ev]
      ON MATCH SET  r.evidence_ids = CASE WHEN $ev IN r.evidence_ids
                                          THEN r.evidence_ids
                                          ELSE r.evidence_ids + [$ev] END
    """, {"src": src, "dst": dst, "ev": ev_id, "conf": conf, "vf": valid_from, "source": source})
```

Running the same document twice must change nothing (add a test).

### 7.2 Functional relations: supersession vs conflict

Functional relations (a person has one active team; a system has one active owning team; a decision supersedes at most one):

```
new fact arrives for functional relation R on subject S, with source date D_new, authority A_new
existing active edge has valid_from D_old, authority A_old

if D_new > D_old:                       # normal change over time
    close old edge: status='superseded', valid_to=D_new
    create new edge: status='active'
elif D_new == D_old or A comparison ambiguous:
    mark both status='disputed'
    create (:Conflict {type:'ownership', status:'open'})-[:INVOLVES]->(S), ...
else:                                   # out-of-order older info
    store as superseded history, valid_to = D_old
```

Conflicts are first-class and visible. The agent mentions them in answers and the UI has a Conflicts tab.

```cypher
// close previous active team on newer membership
MATCH (p:Person {id:$pid})-[r:MEMBER_OF {status:'active'}]->(t:Team)
WHERE t.id <> $new_team AND $doc_date > r.valid_from
SET r.status='superseded', r.valid_to=$doc_date
```

### 7.3 Decision supersession

When extraction yields `ADR-12 SUPERSEDES ADR-7`, set `ADR-7.status='superseded'` and keep it queryable. The agent's `trace_decision` walks the chain.

### 7.4 Ingest result object (feeds the UI's "what changed" panel)

```json
{
  "doc_id": "doc_meet_1020",
  "nodes_added": ["Decision:ADR-15"],
  "edges_added": [["p_priya","OWNS","sys_retry"]],
  "edges_superseded": [["p_priya","OWNS","sys_gateway"]],
  "conflicts_opened": [],
  "facts_rejected": 2,
  "touched_entity_ids": ["p_priya","sys_retry","sys_gateway","dec_adr15"]
}
```

---

## 8. Graph analytics layer

Run after each ingest on the touched subgraph (or the whole graph, which is small).

### 8.1 Inferred `KNOWS_ABOUT` (expertise)

Score per (person, topic or system), with a stored breakdown for explainability:

```
score(p, t) = Σ over tickets x about t:
                (3.0 if p FIXED x, 1.0 if p ASSIGNED_TO x)
                × exp(-age_days(x) / 365)
            + 2.0 × (# postmortems/ADRs about t authored by p)
availability multiplier: 1.0 if p.active and has an active MEMBER_OF, else 0.3
```

```cypher
MATCH (t:Ticket)-[:ABOUT]->(:Topic {id:$topic})
MATCH (p:Person)-[r:FIXED|ASSIGNED_TO]->(t)
WITH p, t, type(r) AS rel,
     CASE type(r) WHEN 'FIXED' THEN 3.0 ELSE 1.0 END AS w,
     ($now_ms - t.resolved_at) / 86400000.0 AS age_days
WITH p, collect({ticket:t.key, rel:rel, w:w}) AS contribs,
     sum(w * exp(-age_days / 365.0)) AS score
OPTIONAL MATCH (p)-[m:MEMBER_OF {status:'active'}]->(team:Team)
RETURN p.id AS id, p.name AS person, score, contribs,
       collect(team.name) AS current_teams
ORDER BY score DESC
LIMIT $k
```

Pass `$now_ms` as a parameter (fixed "demo today") so results are reproducible.

### 8.2 `SIMILAR_TO` between tickets

Two tickets are similar if they share a `Topic` *and* an affected `System` (or a dependency neighbour).

```cypher
MATCH (a:Ticket)-[:ABOUT]->(t:Topic)<-[:ABOUT]-(b:Ticket)
WHERE a.id < b.id
OPTIONAL MATCH (a)-[:AFFECTS]->(s:System)<-[:AFFECTS]-(b)
WITH a, b, count(DISTINCT t) AS shared_topics, count(DISTINCT s) AS shared_systems
WHERE shared_topics >= 1
MERGE (a)-[r:SIMILAR_TO]->(b)
SET r.score = shared_topics + 2*shared_systems, r.source='inferred'
```

### 8.3 Algorithms as agent tools

```cypher
// critical systems
CALL algo.pageRank('System', 'DEPENDS_ON') YIELD node, score
RETURN node.name AS system, score ORDER BY score DESC LIMIT 5

// knowledge bottlenecks (people who connect otherwise separate groups)
CALL algo.betweenness({nodeLabels:['Person','Team','Project'],
                       relationshipTypes:['MEMBER_OF','WORKED_ON']})
YIELD node, score RETURN node.name, score ORDER BY score DESC LIMIT 5

// shortest explanation path
MATCH (a:Ticket {key:$ticket}), (b:Person {id:$person})
MATCH p = shortestPath((a)-[*..6]-(b))
RETURN p
```

Also available per the FalkorDB algorithms docs: BFS, weakly connected components, community detection (CDLP), single-source shortest paths. Use one more (WCC or CDLP) if you want a "clusters of related work" view. Verify exact procedure names in your version.

---

## 9. LLM Wiki generation

### 9.1 Principle

A wiki page is `render(fact_pack(entity))`. The fact pack comes from Cypher, so the page can't contain a claim the graph doesn't have.

### 9.2 Page types and fact packs

| Page | Fact pack contents (all via Cypher) |
|---|---|
| Project | owners (active + history), systems, open/resolved tickets, decisions affecting it, incidents, dependencies, conflicts, evidence ids |
| Person | team (active + history), owned things, tickets fixed, decisions made, `KNOWS_ABOUT` top topics with breakdown |
| System | owners, dependencies and dependents, PageRank rank, incidents, tickets, decisions |
| Decision | rationale, decider, date, supersession chain, affected projects/systems, source doc |
| Topic / recurring issue | tickets, fixers, systems, decisions, similar-ticket cluster |

Example fact pack (JSON the generator receives):

```json
{
  "entity": {"id":"proj_gateway","type":"Project","name":"Payment Gateway"},
  "owners": [{"id":"team_platform","name":"Platform","status":"active","valid_from":"2026-03-01","evidence":["ev_91"]},
             {"id":"team_payments","name":"Payments","status":"superseded","valid_to":"2026-03-01","evidence":["ev_12"]}],
  "decisions": [{"id":"dec_adr12","title":"Use Vendor B API","decided_at":"2026-01-10","supersedes":"dec_adr7","evidence":["ev_40"]}],
  "tickets": [{"key":"BUG-123","status":"resolved","fixed_by":"Priya Sharma","evidence":["ev_55"]}],
  "conflicts": []
}
```

### 9.3 Rendering: template for structure, LLM only for prose

- **Deterministic (template):** Owners, Dependencies, Key decisions list, Known issues table, Related pages, Sources. Zero hallucination risk.
- **LLM-written (constrained):** "Overview" and "History" paragraphs. Prompt: *use only the facts in the pack; end every sentence with an evidence tag `[E:ev_id]`; no outside knowledge.*
- **Post-validation:** parse every `[E:...]` tag. Reject the page if a tag isn't in the pack or if a sentence has no tag. Retry once, then fall back to the template-only page.
- **Render tags** to links pointing at the evidence drawer or source URI.

Page layout:

```markdown
---
entity_id: proj_gateway
type: Project
fact_hash: 9f3c...e1
generated_at: 2026-10-14T12:00:00Z
---
# Payment Gateway
> Generated from the knowledge graph. Illustrative synthetic data.

## Overview
… [E:ev_12][E:ev_91]

## Owners
| Owner | Status | Since | Source |
## Key decisions
## Known issues
## Dependencies
## History
## Open conflicts
## Related pages
## Sources
```

### 9.4 Staleness and regeneration (the "living" part)

- `fact_hash = sha256(canonical_json(fact_pack))`, stored on the `WikiPage` node.
- On ingest, compute affected pages (Cypher below), rebuild each fact pack, compare hashes, and regenerate only the changed pages.
- Save the old version; the UI shows a **diff** of the regenerated pages.
- `(WikiPage)-[:SUMMARIZES]->(entity)` and `(WikiPage)-[:LINKS_TO]->(WikiPage)` make the wiki itself graph-navigable.

```cypher
MATCH (e) WHERE e.id IN $touched
OPTIONAL MATCH (e)--(n)
WITH collect(DISTINCT e) + collect(DISTINCT n) AS ents
UNWIND ents AS x
MATCH (w:WikiPage)-[:SUMMARIZES]->(x)
RETURN DISTINCT w.id AS page_id
```

### 9.5 How the agent uses the wiki

Only for narrative context ("what should someone know before touching this?"). Claims in answers must still cite evidence ids from graph tools. This keeps the wiki from becoming an uncited second truth.

---

## 10. The Company Brain agent

### 10.1 Tools (all read-only; each returns data + trace + evidence ids)

| Tool | What it does | Backed by |
|---|---|---|
| `resolve_entities(text)` | Map names/keys in the question to node ids | Full-text index + aliases |
| `get_neighborhood(id, depth, rel_types?)` | Local subgraph | Cypher |
| `find_paths(a, b, max_hops)` | Explanation path between two entities | `shortestPath` / `allShortestPaths` |
| `find_experts(topic_or_system, k)` | Ranked people with score breakdown, current team | §8.1 Cypher |
| `similar_issues(ticket_or_text)` | Similar tickets and who fixed them | `SIMILAR_TO`, topics |
| `trace_decision(id)` | Supersession chain, rationale, decider, source quotes | Cypher |
| `impact_of(system_id)` | Dependents (1-3 hops) and their owners | Variable-length match |
| `rank_importance(label)` | PageRank/betweenness results | `algo.*` |
| `list_conflicts(entity_id?)` | Open conflicts and the competing evidence | Cypher |
| `get_evidence(ids)` | Quotes + document links | Cypher |
| `read_wiki(page_id)` | Generated narrative | Files |
| `run_cypher_ro(query)` | Guarded fallback | `ro_query` + allowlist |
| `final_answer(...)` | Structured answer (see 10.3) | n/a |

Guard for `run_cypher_ro`: use `g.ro_query`, reject any query containing `CREATE|MERGE|DELETE|SET|REMOVE|DROP|LOAD|CALL` (except an allowlist of `algo.*` and `db.idx.fulltext.queryNodes`), add `LIMIT` if missing, set a timeout.

### 10.2 Tool return contract

```python
@dataclass
class ToolResult:
    data: dict | list
    trace: list[dict]          # [{"src":"BUG-123","rel":"FIXED","dst":"p_priya","src_label":"Ticket","dst_label":"Person"}]
    evidence_ids: list[str]
```

The loop merges every tool's `trace` and `evidence_ids` into a `RunState`. The UI highlights the path from `RunState.trace`.

### 10.3 Final answer schema (forces structure)

```python
class Claim(BaseModel):
    text: str
    evidence_ids: list[str]            # must be non-empty
    path: list[dict] = []              # subset of RunState.trace

class FinalAnswer(BaseModel):
    summary: str
    claims: list[Claim]
    recommended_entities: list[str] = []
    caveats: list[str] = []            # conflicts, superseded info, low confidence
```

### 10.4 Loop

```python
import anthropic, json
client = anthropic.Anthropic()

SYSTEM = """You are Company Brain. Answer ONLY from tool results.
Plan: identify entities, choose graph tools, gather evidence, then call final_answer.
Prefer find_experts/trace_decision/impact_of over raw Cypher. Mention conflicts and
superseded information in caveats. Every claim needs evidence_ids returned by tools."""

def run_agent(question, tools, tool_specs, model, max_steps=8):
    state = RunState()
    msgs = [{"role": "user", "content": question}]
    for _ in range(max_steps):
        resp = client.messages.create(model=model, max_tokens=2000,
                                      system=SYSTEM, tools=tool_specs, messages=msgs)
        msgs.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            break
        results = []
        for c in calls:
            if c.name == "final_answer":
                return verify_and_return(FinalAnswer(**c.input), state)
            out = tools[c.name](**c.input)             # ToolResult
            state.absorb(c.name, c.input, out)         # trace + evidence + step log
            results.append({"type": "tool_result", "tool_use_id": c.id,
                            "content": json.dumps(out.data)[:8000]})
        msgs.append({"role": "user", "content": results})
    return fallback_answer(state)
```

### 10.5 Verifier (the explainability guarantee)

```python
def verify_and_return(ans, state):
    kept, dropped = [], []
    for c in ans.claims:
        ok = c.evidence_ids and set(c.evidence_ids) <= state.evidence_ids
        path_ok = all(edge in state.trace for edge in c.path)
        (kept if ok and path_ok else dropped).append(c)
    ans.claims = kept
    if dropped:
        ans.caveats.append(f"{len(dropped)} statement(s) removed: no supporting evidence retrieved.")
    return ans, state           # UI gets answer + full step log + trace
```

### 10.6 Intent guidance (put in the system prompt, not hard-coded routing)

| Intent cue | Preferred tool chain |
|---|---|
| "who can help / who knows" | `resolve_entities` → `similar_issues` → `find_experts` → `find_paths` → `get_evidence` |
| "why did we / who decided" | `resolve_entities` → `trace_decision` → `get_evidence` |
| "who owns" | `resolve_entities` → `get_neighborhood(OWNS)` → `list_conflicts` |
| "what breaks / impact" | `resolve_entities` → `impact_of` → `rank_importance` |
| "risk / single point" | `rank_importance(betweenness)` + bus-factor query |
| "what changed" | graph edges with recent `valid_from`, wiki diffs |

### 10.7 Example run (hero question)

Question: *"Who can help us fix the payment timeout issue, and why are they the right person?"*

1. `resolve_entities` → Topic `timeouts`, System `Payment Gateway`
2. `similar_issues` → BUG-123 (Gateway), BUG-201 (Auth Service)
3. `find_experts(topic=timeouts)` → Priya 6.4 (fixed BUG-123, authored INC-14), Rahul 4.1 (fixed BUG-201)
4. `find_paths(BUG-123, p_priya)` → BUG-123 ←FIXED– Priya; BUG-123 –RECORDED_IN→ INC-14 ←AUTHORED– Priya
5. `list_conflicts(sys_gateway)` → ownership conflict, Payments vs Platform
6. `final_answer`: Priya is the strongest match (score breakdown + evidence quotes). She now sits on Platform, so loop in current Gateway owner X. Rahul is the backup via BUG-201 and the Auth dependency. Caveat: ownership of the Gateway is disputed (two sources).

### 10.8 MCP server (cheap bonus, aligns with the sponsor stack)

```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("company-brain")

@mcp.tool()
def find_experts(topic: str, k: int = 3) -> dict:
    """Rank people by demonstrated expertise on a topic or system, with score breakdown."""
    return tools.find_experts(topic, k).data

# register trace_decision, impact_of, find_paths, ... the same way
if __name__ == "__main__":
    mcp.run()
```

The web agent and the MCP server share one `tools.py`.

---

## 11. Essential Cypher (copy into README)

```cypher
-- 1. Who knows about this system (from the brief's example, adapted)
MATCH (p:Person)-[:WORKED_ON]->(:Project)-[:HAS_TICKET]->(:Ticket)-[:AFFECTS]->(s:System {id:$system})
RETURN p.name AS person, count(*) AS tickets
ORDER BY tickets DESC LIMIT 5

-- 2. Decision lineage with sources
MATCH path = (d:Decision {id:$id})-[:SUPERSEDES*0..5]->(old:Decision)
OPTIONAL MATCH (old)-[:MADE_BY]->(p:Person)
OPTIONAL MATCH (old)-[:RECORDED_IN]->(doc:Document)
RETURN old.id, old.title, old.decided_at, old.status, old.rationale, p.name AS decided_by, doc.uri
ORDER BY old.decided_at DESC

-- 3. Impact analysis
MATCH (s:System {id:$id})<-[:DEPENDS_ON*1..3]-(dep:System)
OPTIONAL MATCH (team:Team)-[o:OWNS {status:'active'}]->(dep)
RETURN dep.name AS dependent, collect(DISTINCT team.name) AS owners

-- 4. Bus factor
MATCH (s:System)
OPTIONAL MATCH (p:Person)-[k:KNOWS_ABOUT]->(s) WHERE k.score >= $min_score
WITH s, count(p) AS experts, collect(p.name) AS who
WHERE experts <= 1
RETURN s.name AS system, experts, who

-- 5. Open conflicts with evidence
MATCH (c:Conflict {status:'open'})-[:INVOLVES]->(e)
RETURN c.type, c.note, collect(e.name) AS involved

-- 6. Evidence for a set of ids
MATCH (ev:Evidence)-[:FROM]->(doc:Document)
WHERE ev.id IN $ids
RETURN ev.id, ev.quote, doc.title, doc.uri, doc.kind, doc.date

-- 7. Ownership history (as-of query)
MATCH (t:Team)-[r:OWNS]->(x {id:$id})
WHERE r.valid_from <= $as_of AND (r.valid_to IS NULL OR r.valid_to > $as_of)
RETURN t.name, r.status, r.valid_from, r.valid_to, r.evidence_ids

-- 8. Entity lookup (resolution)
CALL db.idx.fulltext.queryNodes('Person', $text) YIELD node, score
RETURN node.id, node.name, node.aliases, score ORDER BY score DESC LIMIT 5

-- 9. PageRank / betweenness / shortestPath: see §8.3
-- 10. Expert finder: see §8.1
-- 11. Affected wiki pages: see §9.4
```

---

## 12. API and UI

### 12.1 FastAPI endpoints

| Endpoint | Purpose |
|---|---|
| `POST /ingest` (file or text) | Run ingest → extract → write → analytics → wiki regen. Returns the ingest result object |
| `POST /ask` | Run agent. Returns `FinalAnswer`, step log, trace, evidence |
| `GET /graph/subgraph?ids=…&depth=1` | Nodes/edges for Cytoscape |
| `GET /wiki/{page_id}` / `GET /wiki/{page_id}/diff` | Page + diff vs previous |
| `GET /evidence/{id}` | Quote + document link |
| `GET /conflicts` | Open conflicts |
| `POST /demo/reset` | Reload baseline dataset, clear updates |

Stream `/ask` progress (SSE) so the UI shows tool steps live. It looks great and masks latency.

### 12.2 UI layout (one page)

```
┌───────────────────────────┬──────────────────────────────────────┐
│ CHAT                      │ GRAPH (Cytoscape)                    │
│ question box              │ answer path highlighted, nodes       │
│ answer + claims [E:..]    │ coloured by label, click = details   │
│ "How I got this" steps    │                                      │
├───────────────────────────┼──────────────────────────────────────┤
│ EVIDENCE DRAWER           │ TABS: Wiki · Conflicts · Ingest/Diff │
│ quote · doc link · edge   │                                      │
└───────────────────────────┴──────────────────────────────────────┘
```

Must-haves: clicking a claim highlights its path and opens its evidence; Ingest tab shows graph delta + wiki diff + rejected facts.

---

## 13. End-to-end flows

### 13.1 Flow A: bootstrap

```
reset graph → create indexes → Lane-1 structured load (org.csv, tickets.json)
→ for each prose doc: chunk → LLM extract → verify → resolve → write
→ run analytics (KNOWS_ABOUT, SIMILAR_TO, PageRank)
→ generate all wiki pages (fact pack → render → validate)
→ ready
```

### 13.2 Flow B: question

```
question → agent loop (≤8 tool steps) → RunState(trace, evidence)
→ final_answer → verifier drops unsupported claims
→ UI: answer, highlighted path, evidence drawer, step log
```

### 13.3 Flow C: live update (the money shot)

```
upload data/updates/meeting_notes_oct20.md
→ extraction finds: Priya OWNS Retry Policy; ADR-15 timeout=8s affecting Gateway
→ writer: closes old edges, adds new ones, opens/clears conflicts
→ touched entities → affected pages → fact_hash compare → regenerate 4 pages
→ UI: "+3 edges, 1 superseded, 4 pages updated" with diffs
→ re-ask the hero question → answer or caveats change, now citing the new doc
```

---

## 14. Evaluation (small but decisive)

**Set:** 15-20 questions across the five types in §2, each with gold answer, gold evidence ids, and minimum hops (from `GROUND_TRUTH.yaml`).

**Baseline:** chunk-level search (FalkorDB full-text over `Evidence`/chunks, or BM25) feeding the same LLM with the top-k chunks and no graph tools.

**Metrics:**

| Metric | Definition |
|---|---|
| Answer correct | Matches gold (LLM judge with rubric, plus spot-check by hand) |
| Citation precision | Cited evidence actually supports the claim |
| Path validity | Every path edge exists in the graph |
| Multi-hop accuracy | Accuracy on questions needing ≥3 hops |
| Staleness test | After the update doc, answer reflects new fact (graph agent) vs not (baseline with old index) |

Put one results table in the README. Even 15 questions is persuasive. Also add unit tests for: ingest idempotency, quote verification, supersession logic, and verifier behaviour.

---

## 15. Repo structure

```
company-brain/
├── README.md                 # problem, architecture, data model, Cypher, run, demo link
├── docker-compose.yml
├── .env.example
├── pyproject.toml
├── data/
│   ├── synthetic/{docs,meetings,chats}/  tickets.json  org.csv  GROUND_TRUTH.yaml
│   └── updates/              # held-out docs for the live demo
├── brain/
│   ├── config.py
│   ├── graph/{client.py, schema.py, writer.py, analytics.py, queries/*.cypher}
│   ├── ingest/{loader.py, chunker.py, registry.py}
│   ├── extract/{schemas.py, llm.py, extractor.py, verify.py, resolve.py}
│   ├── update/{pipeline.py, impact.py, conflicts.py}
│   ├── wiki/{factpack.py, render.py, generate.py, validate.py}
│   ├── agent/{tools.py, loop.py, verifier.py, state.py}
│   ├── api/main.py
│   └── mcp_server.py
├── ui/{index.html, app.js, styles.css}
├── wiki/                     # generated markdown (gitignored or committed as sample)
├── eval/{questions.yaml, run_eval.py, baseline.py, results.md}
└── tests/
```

---

## 16. Build plan

Hackathon length isn't in your paste. The plan is in **blocks**; scale each to your actual window. Before kickoff, limit yourself to what the rules allow (planning, this doc, dataset ideas). Check the rules, since main coding must start after the event begins.

| Block | Deliverable | Done when |
|---|---|---|
| 1. Foundation | Docker + client, schema, indexes, synthetic dataset + `GROUND_TRUTH.yaml`, Lane-1 structured load | Graph visible in FalkorDB Browser (port 3000) with people/teams/tickets |
| 2. Extraction | Pydantic schemas, extractor, quote verifier, resolver, writer, evidence nodes, supersession/conflict logic | All synthetic docs ingest, re-ingest is a no-op, planted conflict and supersession appear |
| 3. Analytics + agent | `KNOWS_ABOUT`, `SIMILAR_TO`, tools, loop, verifier, trace | Hero question returns the right person with path and evidence |
| 4. Wiki | Fact packs, templates, constrained LLM prose, validation, `WikiPage` nodes, regeneration by hash | Pages generate; changing one fact regenerates only affected pages |
| 5. UI + API | Chat, Cytoscape path highlight, evidence drawer, ingest/diff tab, SSE | Full flow works from the browser |
| 6. Live update + eval | Update pipeline demo, eval run vs baseline, tests | Table in README; Flow C demo is reliable |
| 7. Polish | README (data model, Cypher), demo video, public repo, MCP server | Submission checklist ticked |

**Team of four:**

| Role | Owns |
|---|---|
| A: Graph/ingest | Schema, writer, extraction, resolver, conflicts |
| B: Wiki/update | Fact packs, rendering, validation, regeneration, diff |
| C: Agent/eval | Tools, loop, verifier, MCP, eval + baseline |
| D: UI/data/story | Synthetic data, UI, README, demo video |

**Solo:** do Blocks 1-3 and 5 first, a thinner wiki (template-only plus LLM overview), and keep the eval at ~10 questions. Skip MCP unless time remains.

---

## 17. Demo script (about 3 minutes) and submission checklist

**Script**

1. **(20s) Problem:** scattered docs, tickets, chats; people leave; nobody knows why decisions were made.
2. **(50s) Hero question:** ask who can fix the payment timeout. Show the step log, highlighted path, click a claim → evidence quote and source doc. Point out the caveat about the ownership conflict.
3. **(30s) Decision trace:** "Why Vendor B?" Show the superseded ADR chain.
4. **(45s) Living knowledge:** upload the Oct 20 notes. Show graph delta, 4 wiki page diffs, and re-ask: the answer changes.
5. **(20s) Graph algorithms:** "bus-factor risks" and PageRank critical systems.
6. **(15s) Eval table:** graph agent vs search-only baseline.

**Submission checklist** (confirm exact requirements on the hackathon page):

- [ ] Public repository
- [ ] FalkorDB clearly central: the agent can't answer without graph tools
- [ ] README includes: problem, architecture diagram, **graph data model**, **essential Cypher queries/algorithms**, setup (`docker compose up`), eval results
- [ ] Demo video
- [ ] Only synthetic or permitted data; labelled as synthetic
- [ ] Commit history shows work starting after kickoff

---

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Extraction is noisy | Structured lane for structured data; quote verification; confidence floor; rejected-fact log |
| Entity duplicates ruin paths | Deterministic resolver first; graph-aware adjudication; alias arrays; manual merge list for the demo set |
| LLM latency in live demo | Pre-compute baseline graph/wiki; stream steps; only the update doc runs live; cache extraction results keyed by chunk hash with a recorded fallback |
| Agent wanders | `max_steps`, intent guidance, tools that return compact results, structured final answer |
| Wiki hallucination | Template for structure, tag-validated prose, fallback to template-only |
| Cypher syntax differs by version | Pin the image tag (`falkordb/falkordb:<version>`), test queries in Browser early |
| Judges say "it's RAG" | Hero questions that require algorithms/multi-hop; eval vs baseline; path + evidence UI |
| Scope creep | §1.4 cut list; Block gates |

---

## 19. Stretch ideas (only after the core works)

- **Agent memory in the graph:** log `(:Query)-[:USED]->(entity)`; thumbs up/down adjusts `KNOWS_ABOUT.score`. Gives you "remember across sessions" and "share state between agents" from the brief.
- **Two agents sharing the graph:** an Ingest agent and an Answer agent, with a `Task` node handoff ("document needs review" → wiki regenerated).
- **As-of questions:** "who owned the Gateway in March?" via `valid_from/valid_to`.
- **Multi-tenant demo:** two graphs, same code, isolated answers.
- **Community detection** to auto-suggest team/project clusters and new wiki index pages.
- **Slack-style connector** that replays a chat log as a stream.

---

## 20. Positioning line

> **Company Brain** turns scattered company knowledge into a FalkorDB graph and an always-current wiki. Every answer comes with the evidence behind it and the exact graph path that led there, and when new information arrives, the knowledge updates itself.