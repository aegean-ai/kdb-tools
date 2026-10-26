# What the kDB takes from Palantir Foundry's Ontology

Palantir built Foundry around an ontology, and its documentation is explicit about why each piece
is the way it is. The kDB ontology layer ([`design.md`](design.md)) adopts most of those choices.
This page records each choice, Foundry's stated reason, and what the kDB does with it, so that a
later change to the kDB can be weighed against the argument that produced it.

Sources are Foundry's public documentation, read on 2026-10-25. Each claim about Foundry links
the page it comes from. Statements about the kDB are our design, not Foundry's.

## 1. Types and instances are separate layers

Foundry's backend keeps the definition of what exists apart from the data. The Ontology Metadata
Service "defines the set of ontological entities that exist", and object databases hold the
indexed instances ([object backend][backend]). Each ontology concept has a dataset analog: object
type to dataset, object to row, property to column, link type to join
([core concepts][core]). Foundry stresses that the ontology is bound to real data, not an abstract
model ([properties][props]), and that the indexed object data is ephemeral, rebuilt from the
datasources and the recorded edits ([how edits are applied][edits]).

The kDB takes the same split. The kDB is the metadata service: classes, properties, constraints,
link types, action types. The DuckLake catalog and its Parquet files in R2 are the datasets. A
kDB page that only makes sense for one object is in the wrong layer.

## 2. Every object type is bound to a table with a stable key

An object type in Foundry is backed by a datasource whose columns map to properties, and it has a
primary key and a title key. One datasource backs one object type. Foundry warns that keys
"should be deterministic": if a key changes between builds, "edits can be lost and links may
disappear", so keys are derived from columns, never from row numbers or random values
([create an object type][create-ot]). An object type may take its properties from several
datasources joined on the key, with each property coming from exactly one of them
([multi-datasource objects][mdo]).

A kDB class page therefore declares its table, key column, title column and the column to
property map. The sync check rejects keys that are not derived from columns. A class can draw on
several tables, one per property group, which suits measurements that arrive in separate Parquet
tables.

## 3. Constraints live in versioned value types

A Foundry value type is a "semantic wrapper around a field type that include[s] metadata and
constraints", so that users "define and consume meaning directly from the value type, rather than
relying on ... column names or property descriptions" ([value types][vt]). Constraints cover
ranges, enums, patterns and nested fields ([constraints][vt-c]). The base type and the
constraints of a published version are immutable; changing them creates a new version, and a
breaking change with existing consumers means deprecating the type and creating a new one
([versions][vt-v]).

The kDB's constraint (a nominal value, a tolerance and a unit, citing evidence) is a value type in
this sense, and it is versioned the same way. Foundry's units are display formatting only
([value formatting][fmt]). In the kDB the unit is part of the type, because a tolerance with no
unit cannot be checked. Every kDB constraint also cites the evidence for its numbers, which
Foundry's value types do not carry.

Shared properties follow Foundry: the metadata is shared across object types, "the underlying
object data is not" ([shared properties][shared]).

## 4. Media is referenced, not copied

A Foundry media reference points to an item in a media set by identifier and media type. It keeps
working after the path is overwritten, because it names the item, not the location
([media sets][media]). Files uploaded through an action reach the media set only once the
submission succeeds, so a failed action leaves nothing behind ([media in the ontology][media-ont]).

The kDB cites media by content hash, so a citation can be checked against the bytes. It keeps
Foundry's orphan rule. Foundry does not allow lists: "Media reference lists are not supported as a
property type on an object" ([media in the ontology][media-ont]). A kDB claim often rests on
several frames or regions, so the kDB allows them.
Foundry also notes that virtual media sets do not notice deletions at the source
([virtual media sets][vms]); hash addressing lets the kDB sync check detect a missing file and
flag it.

## 5. Interfaces carry the generic types

An interface "describes the shape of an object type and its capabilities" and gives "object type
polymorphism". It has no data of its own. Object types implement it by mapping their properties,
links and actions onto its requirements, and a search on the interface returns objects of every
implementing type ([interfaces][iface]). Interface link constraints can target another interface,
so two abstract types can be related before any concrete type exists
([interface link types][iface-link]). Foundry's guidance calls interfaces "the primary tool for
achieving the 'Do not repeat yourself' design principle and open/closed extensibility"
([structural guidance][guide]).

The kDB's generic robotics types are interfaces: `Link`, `Joint`, `Actuator`, `Gripper`. Many
specific link and joint types implement them, across AR4 MK4, MK5 and other arms, and a check
written against `Joint` works for a joint type added later. `Joint` declares required links to
`Link` for its parent and child.

## 6. A joint is an object-backed link

Foundry offers three backings for a link type: a foreign key, a join table, or an intermediary
object type that carries its own properties, as a flight manifest sits between an aircraft and a
flight ([create a link type][link]). Foundry does not enforce one-to-one cardinality
([create a link type][link]).

A kinematic joint is that third kind. It sits between a parent link and a child link and has an
axis, limits and offsets of its own, so the kDB models `Joint` as a class with two many-to-one
links rather than as a bare edge. Unlike Foundry, the kDB sync check enforces that every child
link has exactly one parent joint, because a kinematic tree breaks otherwise.

## 7. Changes happen through typed actions, with a log

"An action is a single transaction that changes the properties of one or more objects, based on a
user-defined logic", and Foundry wants users thinking "about overall objectives instead of
specific property edits" ([action types][actions]). New object types accept edits through actions
only by default, "to enforce a consistent security paradigm" ([permissions][act-perm]).

The parts of a Foundry action type that the kDB copies:

| Foundry | Source | kDB |
|---|---|---|
| typed parameters, constrained by value types | [parameters][act-param] | Pydantic parameter models |
| submission criteria, each with its own failure message | [submission criteria][act-sub] | preconditions with readable messages |
| ACID transaction, snapshot isolation by default, a concurrent write fails one action | [consistency][act-acid] | one commit per action, optimistic version check, reject on conflict |
| test run returns the edits without applying them | [test run][act-test] | dry run returning the diff, called by agents before submitting |
| action log: one logged object per submission with type, version, time, user, edited keys and parameters | [action log][act-log] | an `action_log` lakehouse table with those fields plus agent and model ids and the cited evidence |
| edit history kept separately from the decision log | [edit history][hist] | git history of the pages |
| revert only by the same user and only while it is the latest edit | [reverts][act-rev] | `RevertAction` with the same rule |
| writeback before the edit blocks it; side effects after it do not | [webhooks][act-hook] | exports and rendering run after the commit and may retry |

Foundry names "Action Sprawl", many single-property "Set X" actions, as an anti-pattern that
fragments workflows and audit trails ([anti-patterns][anti]). kDB actions are named after
operations such as `ReviseConstraint` and `ResolveFlag`, not after fields.

Foundry's actions edit instances. Its schema changes go through Ontology Manager saves, branches
and proposals ([save changes][save], [branching][branch]). kDB actions edit types, so they need
the action machinery above and the governance in section 9 together.

## 8. Source data and edits: our stricter rule

When a datasource update and a user edit disagree, Foundry resolves it per datasource, either
"apply user edits" (the default, edits always win) or "apply most recent value"
([how edits are applied][edits]).

The kDB never resolves it automatically. New lakehouse evidence opens a flag, and only a
`ResolveFlag` action, which cites the flag, changes the kDB. In effect this is Foundry's default
with a review queue in front of it.

## 9. Governance: statuses, review and migrations

Every Foundry type has a status: experimental (the default), active or deprecated. An active
resource cannot be deleted and its API name cannot change, and a deprecated one records a reason,
a deadline and a replacement ([statuses][status]). Changes to protected resources go through a
branch and a proposal, which Foundry describes as "analogous to a Pull Request", reviewed
resource by resource ([branching][branch], [proposals][proposal]). A breaking change (a datasource,
a key, a property type, a deleted property) cannot be saved until a migration for existing edits
is chosen ([schema migrations][migr]). Storing versions as separate objects is "The Time Machine"
anti-pattern ([anti-patterns][anti]).

The kDB uses the same statuses with the same rules. An action on an active type lands through a
pull request on the kDB repository. A breaking kDB change carries a migration for the bound
lakehouse tables. History comes from the action log and git, never from copies of a page.

## 10. Agents use the ontology through actions

In Foundry, "actions can be automatically surfaced as tools for all types of agents". By default
AI agents can only stage actions for human review, and owners later let trusted processes "close
the action loop without human review" ([why an ontology][why]). Agent tools are object queries,
actions (run automatically or after the user confirms) and functions ([chatbot tools][tools]).
Foundry keeps two MCP servers apart: Ontology MCP lets external agents "read objects, execute
predefined actions, and query data", while Palantir MCP changes ontology structure but "cannot
write actual ontology data" ([Ontology MCP][mcp]).

kDB agents (Claude and the students' Pydantic AI agents) change structure as well as data, so
they combine both roles, and actions are their only write path. They get read tools
over the kDB and the lakehouse and the action tools with dry run and submit, and no tool that
writes a page. Agent submissions of `ResolveFlag`, and of any action on an active type, wait for a
person at first, and the policy widens as trust is earned, as Foundry describes.

## Where the kDB departs from Foundry

| Foundry | kDB | Why |
|---|---|---|
| units are display formatting | units are part of the type | tolerances must be checkable |
| no lists of media references | lists allowed | one claim can cite several frames |
| media cited by identifier | media cited by content hash | citations verifiable against bytes |
| one-to-one cardinality not enforced | enforced for the kinematic tree | a link with two parent joints is invalid |
| actions edit instances only | actions edit types too | the kDB is the type layer, and all its changes are actions |
| conflicts resolved by a per-datasource rule | conflicts become flags resolved by an action | no kDB value changes without a logged decision |
| constraints carry no evidence | every number cites evidence | the kDB's purpose is grounded knowledge |

[backend]: https://www.palantir.com/docs/foundry/object-backend/overview/
[core]: https://www.palantir.com/docs/foundry/ontology/core-concepts/
[props]: https://www.palantir.com/docs/foundry/object-link-types/properties-overview/
[edits]: https://www.palantir.com/docs/foundry/object-edits/how-edits-applied/
[create-ot]: https://www.palantir.com/docs/foundry/object-link-types/create-object-type/
[mdo]: https://www.palantir.com/docs/foundry/object-permissioning/multi-datasource-objects/
[vt]: https://www.palantir.com/docs/foundry/object-link-types/value-types-overview/
[vt-c]: https://www.palantir.com/docs/foundry/object-link-types/value-type-constraints/
[vt-v]: https://www.palantir.com/docs/foundry/object-link-types/value-types-versions/
[fmt]: https://www.palantir.com/docs/foundry/object-link-types/value-formatting/
[shared]: https://www.palantir.com/docs/foundry/object-link-types/shared-property-overview/
[media]: https://www.palantir.com/docs/foundry/media-sets-advanced-formats/media-overview/
[media-ont]: https://www.palantir.com/docs/foundry/media-sets-advanced-formats/media-in-ontology/
[vms]: https://www.palantir.com/docs/foundry/media-sets-advanced-formats/virtual-media-sets/
[iface]: https://www.palantir.com/docs/foundry/interfaces/interface-overview/
[iface-link]: https://www.palantir.com/docs/foundry/interfaces/interface-link-types-overview/
[guide]: https://www.palantir.com/docs/foundry/ontology/ontology-structural-guidance/
[link]: https://www.palantir.com/docs/foundry/object-link-types/create-link-type/
[actions]: https://www.palantir.com/docs/foundry/action-types/overview/
[act-perm]: https://www.palantir.com/docs/foundry/action-types/permissions/
[act-param]: https://www.palantir.com/docs/foundry/action-types/parameter-overview/
[act-sub]: https://www.palantir.com/docs/foundry/action-types/submission-criteria/
[act-acid]: https://www.palantir.com/docs/foundry/action-types/consistency-guarantees/
[act-test]: https://www.palantir.com/docs/foundry/action-types/test-run/
[act-log]: https://www.palantir.com/docs/foundry/action-types/action-log/
[hist]: https://www.palantir.com/docs/foundry/object-edits/user-edit-history/
[act-rev]: https://www.palantir.com/docs/foundry/action-types/action-reverts/
[act-hook]: https://www.palantir.com/docs/foundry/action-types/webhooks/
[anti]: https://www.palantir.com/docs/foundry/ontology/ontology-anti-patterns/
[save]: https://www.palantir.com/docs/foundry/ontology-manager/save-changes/
[branch]: https://www.palantir.com/docs/foundry/ontologies/branching-ontology/
[proposal]: https://www.palantir.com/docs/foundry/ontologies/review-ontology-proposals/
[status]: https://www.palantir.com/docs/foundry/object-link-types/metadata-statuses/
[migr]: https://www.palantir.com/docs/foundry/object-edits/schema-migrations/
[why]: https://www.palantir.com/docs/foundry/ontology/why-ontology/
[tools]: https://www.palantir.com/docs/foundry/chatbot-studio/tools/
[mcp]: https://www.palantir.com/docs/foundry/ontology-mcp/overview/
