# Acceptable use

**ONIONTRACE is built for authorised investigative use only.** It supplies *leads*. It does
not supply authority.

## The legal position, stated accurately

Collection is limited to what an anonymous visitor receives from publicly reachable
services. No credentials, no authentication, no enumeration, no forced browsing, no
exploitation. Ingested material is limited to text and metadata; media is recorded as a URL
reference and is never fetched or stored. Deployment is intended for use by an authorised
agency under its own legal process. ONIONTRACE does not assert that collection alone creates
lawful authority to investigate, surveil, or identify any person.

That last sentence is the one that matters. Reading something that was published publicly
is not, by itself, authorisation to build a case about the person who published it.

## Conditions of use

1. **Lawful authority required.** No targeting of any individual without lawful authority
   under the deploying agency's own process.
2. **Not for journalists, activists, or bystanders.** No use against persons who are not
   the subject of an investigation, including anyone whose material appears incidentally.
3. **No redistribution.** Ingested content is not shared, published, or transferred outside
   the deploying agency, and is not used for any purpose other than the case it was
   collected for.
4. **Retention limits as configured.** Every case carries an expiry set when it is opened.
   At expiry, snapshots and derived data are purged, and the purge is logged.
5. **Candidates are not conclusions.** The tool never asserts that two artefacts belong to
   one person. Output is a candidate link with a score, a method, and evidence references;
   a human decides, and the record shows which human.
6. **Access follows role.** Candidate links require analyst access or above; export
   requires the case owner; purge requires an administrator. Every action is logged in an
   append-only trail the operator cannot write to.

## What this project does not do

No exploitation. No authentication attempts. No directory enumeration. No traffic
correlation. No guard discovery. No interaction with subjects. No purchase, download, or
possession of illicit material.

These are out of scope **architecturally, not merely by policy**: the code paths do not
exist. The package that gates collection contains no network capability at all, and a test
asserts it — so "we would not do that" is backed by "the code cannot".

## Retention and access notice

Retention and access follow the deploying agency's rules. This tool enforces the technical
subset of them: case-scoped seeds, a recorded authorisation reference per case, a retention
expiry per case, a purge that is logged, and an append-only audit trail.

---

*Acknowledgement of this statement is recorded, once per deployment, before any case can
be opened. Collection is refused until it is acknowledged and a case exists.*
