# kDB ontology layer, media, and lakehouse sync: design

Status: draft for owner approval, 2026-10-25. Companion: [`foundry.md`](foundry.md) records the
Palantir Foundry design choices this design adopts, with sources.

## 1. Intent

A kDB today is a bundle of OKF markdown pages. Each page holds quantities with a unit and a
source, and `okf-validate` and `okf-ground` check them. Two projects use kdb-tools core: the AR4
digital twin (robotics class) and the Physical AI kDB (AI class, the VLA profile).

The AR4 work exposed what the page model cannot do. A spike measured AR4 link lengths from a
render in the assembly manual, against the printed 629 mm reach label, and got a2 within 1 mm of
the DH value. That measurement needs an image region as its evidence, the pixel points that were
picked, and a check that someone else would pick the same points. Nothing in kdb-tools can cite an
image region, let alone a video clip. The pick-and-place design also calls for a judge that grades
policies and writes its results to a lakehouse (DuckLake, described with ML Schema and PROV-O).
Neither the judge nor the lakehouse exists yet; this work builds the lakehouse, and the judge is
built on it later. Nothing would connect those results to the kDB concepts they are evidence for.

The owner's position is that the kDB and the lakehouse cannot exist without each other, and their
updates must stay in sync. This design makes that concrete. The kDB becomes the semantic layer:
it holds types only. The lakehouse holds every instance, including every image, video and
document. Every change to the kDB goes through a typed, logged action.

The same work documents the tasks students do in both classes, so the design favors explicit,
checkable records over convenience.

## 2. Decisions made by the owner

These were settled in the brainstorm and are binding.

1. The kDB is the semantic layer. It stores type information, never instance (object)
   information. The lakehouse stores the Parquet data, and the columns of that data are defined
   in the kDB.
2. Everything lives in the lakehouse: manual PDFs, renders, photos, videos, experiment outputs,
   measurements, arms, twins, runs. Bytes go in an object store and DuckLake's catalog points at
   them.
3. New lakehouse evidence is attached to the concepts it bears on and flagged. It is never
   applied automatically. A person or an agent decides, and the decision is logged.
4. Media is cited by content hash plus a selector: a pixel box for an image region, a start and
   end time for a video clip, a page (and optionally a box) for a document, or nothing for the
   whole file.
5. A size measured from a picture stores the image region, a cited reference dimension, the pixel
   points picked for the reference and for the measured feature, and the result with its
   uncertainty. The check recomputes the arithmetic, confirms the reference is cited, and has a
   vision model re-find the points. A disagreement from the vision model raises a flag; it does
   not fail the check.
6. Concept pages show their media. The cited region is drawn on the page with the measurement
   overlaid (picked points, the vision model's points, the reference), and video clips play in
   place. The citation stays on the page as well.
7. Every concept has an IRI, and there are several designs (AR4 MK4, MK5, other arms), several
   physical arms, and several twins per design. Under decision 1, only types get kDB IRIs; arms,
   twins and their parts are lakehouse rows.
8. The kDB holds constraints: a nominal value with a tolerance and a unit, citing its evidence.
   "Every AR4 MK4 link 2 has a2 = 305 ± 0.5 mm" is a type statement and lives in the kDB. The
   design specification document and the measurements behind it are lakehouse rows.
9. Ontologies matter, and we adopt established ones rather than invent our own. Foundry's design
   choices are adopted where they fit (see `foundry.md`).
10. Typed actions only. No direct edits of kDB pages, including by human authors.
11. The reference solution has its own catalog. Teams are kept apart, and the course can view
    every team's rows in one catalog keyed by team. Each team runs a local RustFS lakehouse, as in
    the summer AI Lakehouse project. Students are never given R2 keys; they submit through their
    shared Google Drive folder.
12. The types-only rule binds the AI class kDB as well. Its project has just been assigned, so
    the churn is acceptable now.

## 3. Goals, non-goals, anti-goals

Goals:

- A kDB that defines classes, interfaces, properties, constraints, link types and action types,
  each with an IRI, and binds each class to a lakehouse table.
- Media as first-class evidence: images, image regions, PDF regions, video clips and whole
  videos, cited by hash and selector and shown on the concept page.
- A sync check that keeps the kDB and the lakehouse consistent in both directions and turns
  every disagreement into a flag.
- Actions as the only write path, usable by people, Claude agents and the students' Pydantic AI
  agents through the same tools.
- Both class projects on the new model.

Non-goals:

- A Foundry clone. There is no object index service, no application builder and no permission
  system beyond what the object store, DuckLake and git provide.
- An OWL reasoner at check time. Constraints are checked by kdb-tools code. Exporting OWL or SHACL
  is allowed as a side effect (section 7.6) but nothing depends on it.
- Storing media bytes in git or in the kDB bundle.

Anti-goals (a failure even if everything technically works):

- A kDB value changes without a logged action that cites its reason.
- The kDB grows with every student team, run or arm. That would mean instances leaked into it.
- A student cannot run the checks without a commercial agent. The vision check and every agent
  path must work with the course's allowed models: local models, or OpenRouter free or capped.
- Solution rows reachable from any team lakehouse, or any student holding a cloud storage key.

## 4. The split

| Layer | Holds | Examples |
|---|---|---|
| kDB (markdown pages in git, compiled) | classes, interfaces, properties, value types and constraints, link types, action types, SKOS terms, the table binding of each class | `RevoluteJoint`; `AR4MK4Link2` with a2 = 305 ± 0.5 mm; property `length` in metres; `ResolveFlag` |
| Lakehouse (DuckLake catalog, Parquet and media bytes in RustFS) | every instance and every decision | the lab arm; team 7's twin version 12 and its link 2; a run; the manual PDF; a render; a measurement; a flag; the action log |

A kDB page describes one type. A lakehouse row describes one object. If a page would only make
sense for one arm, one twin, one paper or one run, it belongs in the lakehouse.

### 4.1 IRIs

The kDB mints type IRIs under a base per bundle, for example
`https://kdb.aegean.ai/ar4/class/ar4-mk4-link-2` and `https://kdb.aegean.ai/core/property/length`.
The lakehouse mints instance IRIs in a key column of each table, under a base per catalog, for
example `https://data.aegean.ai/student/twin/team-7/v12` and
`https://data.aegean.ai/student/artifact/sha256/<digest>`. The solution catalog uses its own base.

The exact host names and path shapes are the builder's call, with three fixed rules: an IRI never
changes once its type is `active` (section 7.5); instance keys are deterministic, derived from
columns, never row numbers or IRIs generated at build time (Foundry's warning, `foundry.md` §2);
and every row's type column names a kDB class IRI. Whether the IRIs resolve to a web page is open
and does not change the build (section 11).

Existing page paths such as `links/link-2.md` stay as file paths. The IRI is declared in the
page frontmatter and is what everything else cites.

### 4.2 Lakehouses

Every lakehouse uses the stack of the AI Lakehouse summer project (`aiml-common/projects/lakehouse`
in eaia): a RustFS container as the S3-compatible object store, and a DuckLake catalog in a DuckDB
file whose data files live in a RustFS bucket. Media follows that project's rule: bytes are
objects in the bucket, and tables hold their references and metadata. Its video fragment index
(one row per fragment, with clip, frame range, time range and statistics) is the model for video
clip citations.

- Team lakehouse. Each team (in the AI class, each student) runs its own RustFS and DuckLake
  locally from a compose file that kdb-tools ships. Teams are isolated by construction, and
  everything works offline. Instance IRIs include the team.
- Submission. Students never hold R2 or any other cloud credentials. A team submits by exporting
  its lakehouse into its shared Google Drive folder: the catalog file, the bucket's objects, and a
  manifest of every object's SHA-256. An export is self-contained, so the grader restores it into
  a local RustFS and DuckLake and checks every hash against the manifest before reading anything.
  The export format and the command that writes it are the builder's call.
- Course view. The course restores each submission and can load them into one read-only catalog
  with every row keyed by team, for grading and comparison. Only course staff hold its storage
  credentials. Whether it lives in R2 or a course-run RustFS is the builder's call.
- Solution lakehouse. Private, built the same way, never shared with students. The reference
  twin, its runs and the grading evaluations. It shares no rows with any team's lakehouse. The
  design evidence (manual pages, renders, the measurements behind constraints) exists on both
  sides, because building it is the students' work and the solution needs its own copy to grade
  against.

Course-side credentials live in 1Password. The service account is read only, so the builder hands
the owner the `op item create` commands.

### 4.3 One kDB per team

Each team (or AI-class student) keeps its own kDB repository, as today, and that kDB is its type
layer: its design classes and constraints, under the shared core module. So there are as many AR4
type layers as teams, each over its own lakehouse. A row names its class by the IRI from its own
team's kDB, and the team's kDB base IRI includes the team. The anti-goal in section 3 is about one
kDB filling with instances, not about the number of kDBs.

A submission is the kDB repository at a commit plus the lakehouse export at a named snapshot.
Grading reads both: `okf-diff` compares the team's classes and constraints with the reference kDB,
class against class, and the grader reads the team's rows and action log at that snapshot (my
call).

### 4.4 Tests without containers

Every command also works against a plain local DuckLake catalog with a local data directory and no
RustFS. Tests and CI fixtures use that mode, so `okf-validate` still runs in seconds (my call).

### 4.5 Cutover

The change is a hard cutover for both projects (my call). The AR4 student repository has not been
released and the AI class project has only just been assigned, so nobody is midway through work
on the page model. The AI class submission is due in early December 2026, so the AI class starter
moves to the new model first, within the first two weeks.

## 5. The ontology

### 5.1 Adopted vocabularies

We adopt, not invent:

| Need | Vocabulary |
|---|---|
| terms and their matches (already in kdb-tools) | SKOS |
| provenance of runs, actions and derived values | PROV-O |
| experiments, runs, evaluations (the judge already uses it) | ML Schema |
| observations and measurements | SOSA/SSN |
| units and quantity kinds | QUDT |
| robot parts and robots | IEEE 1872 CORA |
| media selectors | W3C Web Annotation (`FragmentSelector` with media fragments, `xywh=` and `t=`) |

kdb-tools gains an ontology module that imports these and adds what they lack. It is public and
both class projects import it. Each project kDB holds only its own classes and constraints.

### 5.2 Layers of type

Three layers, from general to specific:

1. Generic types, in the core module. Interfaces such as `Link`, `Joint`, `Actuator`,
   `Gripper`, `Robot`, `Twin`, `Observation`, `MediaArtifact`. Properties attach here and are
   inherited: every `Link` has mass and inertia; every `Joint` has a parent and a child link; every
   `RevoluteJoint` has an axis, limits and a velocity limit. Joint kinds follow URDF: revolute,
   continuous, prismatic, fixed, floating, planar.
2. Kinds of part, in the core module, where build or shape differs: a machined or cast link,
   a tube link (adds wall thickness), a stepper joint behind a belt or a gearbox (adds reduction
   ratio), a hobby-servo gripper. A kind can add properties and constraints.
3. Design types, in a project kDB: `AR4MK4Link2` is a subclass of a level 2 kind and carries
   the class-level constraints. MK5 reuses a level 2 or level 3 type where the part is unchanged
   and subclasses it where it changed.

Following Foundry, the generic layer is built from interfaces, and a kinematic `Joint` is an
object-backed link: a class of its own, with two many-to-one links (`parent`, `child`) to `Link`
and its own properties. The lakehouse validation enforces that a child link has exactly one parent
joint; Foundry does not enforce one-to-one cardinality and a kinematic tree needs it.

The AI class's existing domains (`datasets`, `robotics`, `vla-datasets`, seeded by `okf-seed`)
stay as SKOS terms. Their `robotics` domain covers tasks, benchmarks, robots and model classes;
the new robotics part types sit beside it in the core module, and where a term and a class name
the same thing the term carries a `closeMatch` to the class.

### 5.3 Class pages

A class page declares, in frontmatter:

- `iri`, `status` (experimental, active, deprecated, with reason, deadline and replacement when
  deprecated), `implements` (interfaces) and `subclass_of`;
- its binding: the DuckLake table, the key column, the title column, and a map from each
  column to a property IRI. A class may draw properties from several tables joined on the key,
  each property from exactly one table (Foundry's multi-datasource rule);
- its constraints (section 5.4).

Property pages declare the IRI, the datatype, the QUDT unit or quantity kind, and whether the
property is required. Units are part of the type, not formatting: a tolerance with no unit cannot
be checked. Shared properties (for example `mass`) are defined once and used by many classes.

### 5.4 Constraints

A constraint is a type statement about every instance of a class: a property, a nominal value, a
tolerance (symmetric or as bounds), a unit, and the evidence it rests on. For example:

```yaml
constraints:
  - property: https://kdb.aegean.ai/core/property/dh-a
    nominal: 0.305
    tolerance: 0.0005
    unit: unit:M
    evidence:
      - https://data.aegean.ai/solution/observation/<id>   # the manual measurement
      - https://data.aegean.ai/solution/artifact/sha256/<digest>#page=3   # the DH table
```

Every number in a constraint is cited, including the tolerance. Where no source gives a
tolerance, it cites a logged owner decision (an action, section 7). Constraints are versioned in
Foundry's way: changing a constraint creates a new version, and a change that breaks existing
consumers deprecates the old constraint rather than editing it.

What students and the twin check do with a constraint: a twin's link 2 value, read from its CAD
document by twin-sync, is tested against the constraint. A lakehouse observation outside the
tolerance raises a flag against the constraint.

## 6. Media

### 6.1 Storage and citation

Every file is a row in a `media` table: SHA-256, media type, byte size, object key (derived from the
hash), original name, where it came from (URL, upload, run), and the subjects it shows (a design
class IRI for a manual render, an arm IRI for a lab video). Bytes are written to the object store only after the
action that registers them succeeds, so a failed action leaves no orphans (Foundry's rule).

A citation is a `MediaRef`: the hash, the media type, and an optional selector in W3C media
fragment form: `#page=3`, `#page=3&xywh=120,40,520,180`, `#xywh=...` on an image, `#t=41,48` on a
video. A property or a constraint can cite a list of `MediaRef`s; Foundry forbids lists of media
references, and we need them because one claim can rest on several frames.

The checks cover media: every cited hash exists in `media` and its bytes in the object store match the hash; a
selector is inside the file (page count, image size, video duration). Existing local PDF and
spreadsheet sources in today's bundles move to `media` rows. Quote and cell grounding stays
required where it applies today: a citation of a document passage also carries a W3C
`TextQuoteSelector` holding the quoted text, and a spreadsheet citation carries its cell, so
`ground.quote` and `ground.cell` keep checking them (my call). `okf-ground`'s page and box
grounding becomes one selector kind among several.

### 6.2 Display

A kDB page renders each cited region as a figure: the crop, with any measurement overlaid, and a
caption naming the artifact and the catalog snapshot. A video clip renders as a player limited to
its time range. Rendering is done by kdb-tools from the citation, never from an image pasted
into the page, so the figure cannot drift from the evidence. How it renders (a static site, an
okf viewer, generated images committed beside the page) is the builder's call; the figure must
show the student's points, the vision model's points and the reference distinctly.

### 6.3 Measurements from pictures

A measurement is a `sosa:Observation` row:

- feature of interest: the design class for a manual render, or an arm IRI for a photo of it;
- observed property: a kDB property IRI;
- evidence: a `MediaRef` with a selector;
- the reference: a cited constraint or another observation (for example the printed 629 mm reach,
  itself an observation of the label in the render), with the pixel points on it;
- the pixel points on the measured feature, each with a feature name ("J2 axis center", "flange
  face");
- the result, its unit and its uncertainty; the procedure; who made it and when.

The check recomputes the result from the points and the reference and confirms the reference is
cited. The vision check is a separate activity, linked by PROV-O: given the crop and the feature
names, a vision model returns its own points. Agreement is within a pixel tolerance derived from
the stated uncertainty (about 1 to 2 px for the spike's 1.36 mm/px). Disagreement raises a flag
on the observation. The model, prompt, crop hash and returned points are stored, so the check can
be rerun. The model must be one the course allows; the builder picks a default local model and an
OpenRouter fallback.

An observation is evidence for a constraint. Turning it into a new or revised constraint takes an
action (section 7).

## 7. Actions

### 7.1 The only write path

Every change to the kDB is an action: a typed operation with parameters, preconditions, an edit
set and a log row. People, Claude agents and Pydantic AI agents all use the same actions through
the same tools (a CLI, the MCP server, the Pydantic AI toolset that `okf-mcp` and the agent module
already expose).

Pages stay files in git, because review and history live there. Enforcement: an action writes the
pages it edits and records the resulting content hash of each page in its log row. `okf-validate`
recomputes each page's hash and reports an error for any page whose hash matches no logged
action. A hand edit therefore fails validation until it is redone as an action.

Lakehouse writes that create instances (registering media, recording an observation, opening a
flag) are also actions, logged the same way. Runs written by the judge are the exception: a run is
its own PROV-O activity, and its rows carry the run's identity and its `kdb_reads` (the type IRIs
and versions it read).

### 7.2 Shape of an action type

Following Foundry (`foundry.md` §6):

- typed parameters, validated before anything runs (Pydantic models fit the existing code);
- preconditions with a readable failure message each;
- an explicit edit set, applied as one transaction, with an optimistic check on the version of
  every page and row it edits: a concurrent change makes the action fail, never merge silently;
- a dry run that returns the diff without applying it. Agents call it before submitting;
- a log row in a lakehouse `action_log` table: action id, action type and version, time, actor
  (person or agent), model id for agents, parameters, edited IRIs with before and after versions,
  the evidence cited, the flag resolved if any, and revert status;
- side effects (regenerating an OWL or SHACL export, rebuilding rendered figures, notifying)
  run after the commit, may fail and retry, and never undo the edit.

Actions are named after operations, not fields (Foundry's "action sprawl" anti-pattern). A
starting set, which the builder may refine: `ProposeClass`, `AddProperty`, `AddConstraint`,
`ReviseConstraint`, `ImplementInterface`, `PromoteClass`, `DeprecateClass`, `RegisterMedia`,
`RecordObservation`, `RunVisionCheck`, `OpenFlag`, `ResolveFlag`, `RevertAction`. A revert is
allowed only while the action is the latest edit to what it touched, and does not undo side
effects.

### 7.3 Flags

A flag is a row in a `flags` table: kind, the evidence, the kDB IRIs it bears on, status and the
resolving action. Kinds include: new evidence for a constraint; evidence outside a tolerance; a
newer snapshot changed a cited row; a vision-model disagreement; a run read a type version that
has since changed; an orphaned or missing artifact. `ResolveFlag` is the only path from a flag to
a kDB change, and it must cite the flag. Resolving can mean revising a constraint, accepting
the evidence without change, or rejecting it with a reason.

### 7.4 Agents

Agents get three tool groups: reads over the kDB (classes, interfaces, constraints), reads over
the lakehouse (instances by class, links), and the actions, each with dry-run and submit. A
per-action policy says whether an agent's submission applies at once or waits for a person;
`ResolveFlag` and every action on an `active` class wait for a person at first. No tool writes a
page directly.

### 7.5 Governance

Statuses follow Foundry: experimental (default), active, deprecated. An active type cannot be
deleted or have its IRI changed. A deprecated type records a reason, a deadline and its
replacement. An action that touches an active type lands through a pull request on the kDB repo,
which is the review step. A breaking change (a property's type, a class's key, a rename, a
delete) must carry a migration for the bound lakehouse tables (drop, move, cast) and is blocked
until the matching DuckLake schema change is staged. History comes from the action log and git,
never from versioned copies of a page (Foundry's "time machine" anti-pattern).

## 8. Sync

Two directions, one rule: the kDB defines, the lakehouse reports.

kDB to lakehouse. The class bindings are the schema of the bound tables. `okf-sync` (name is
the builder's call) compares them with the DuckLake catalog and, through an action, stages the
schema changes the kDB requires.

Lakehouse to kDB. The same command reads the catalog and opens flags. It never edits a page.

The check verifies:

- every bound table and column exists, with the datatype and unit the kDB says, and every column
  of a bound table maps to a property (or is listed as unbound on purpose);
- every row's type column names a kDB class, its key is unique and deterministic, and required
  properties are present;
- every cited lakehouse row exists at the cited snapshot, and a newer snapshot that changed it
  opens a flag;
- every `MediaRef` resolves and its bytes match its hash, and its selector is in bounds;
- every observation has its evidence and provenance complete;
- every page's hash matches a logged action (section 7.1);
- every run whose `kdb_reads` names a type version that has since changed is marked stale and
  flagged.

It runs inside `okf-validate`, and also on its own so CI, agents and the judge can call it after a
write.

## 9. Migration of the two projects

AR4. The quantity pages become constraints on design classes (`AR4MK4Link2` and the rest),
under the core `Link` and `Joint` interfaces. The DH values, joint limits and masses keep their
sources, which move to `media` rows. The reference twin's geometry added in pick-place Task 7
(the bar diameters in `links/*.md`) is twin instance data and moves to the solution catalog. The
twin-check rules read constraints instead of quantities.

AI class. Papers, datasets and reported results become lakehouse rows: a `papers` table, a
`datasets` table, and a `results` table whose condition columns (task, benchmark, suite,
embodiment, perturbation) hold Term IRIs and whose evidence is a `MediaRef` into the paper PDF.
The Term pages and their seeds stay. The profile rules `project.result`, `project.dataset` and
`project.trained-on` become checks on those tables' bindings and rows. Watches become queries
over the `results` table. The starter ingest agent writes rows through actions instead of
writing pages. The student-facing starter must reach students before they have built much on the
page model; the builder sequences this migration first or early.

## 10. Order of work

One design, four pieces, built in order. Each piece leaves both projects working.

1. Ontology layer and actions. IRIs, class and property pages with bindings, interfaces and
   the three layers, constraints, the core module with the adopted vocabularies, the action
   machinery and the action log, page-hash enforcement. Migrate the AI class kDB, then AR4.
2. Media. The `media` table, `MediaRef` and selectors, storage by hash, grounding through
   selectors, rendering on pages.
3. Sync and flags. The sync command in both directions, the flags table, `ResolveFlag`, stale
   runs.
4. Vision check. Picture measurements as observations, the arithmetic check, the vision model
   activity and its flags.

## 11. Left to the builder

- Host names and path shapes of IRIs, and whether they resolve to pages.
- Bucket and 1Password item names; the DuckLake catalog database (the catalog metadata store).
- How figures render and where rendered images live.
- The exact action set beyond the starting list, and the agent auto-apply policy beyond the
  defaults in 7.4.
- The default vision model and its OpenRouter fallback.
- Which CORA, SOSA and QUDT terms each core type aligns with, recorded as `closeMatch` or
  `subclass_of` where verified, and left out rather than guessed (the rule `okf-seed` already
  follows).
- Whether OWL and SHACL exports are produced.
