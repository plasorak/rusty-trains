# UK Rail Simulator — Development Roadmap

Each milestone is a thin vertical slice. Every one of them produces a runnable simulator at the end — you should never have a six-month stretch where nothing executes. Capability grows by *widening* the slice, not by completing layers in isolation.

The order is chosen to de-risk the unknowns first. The two highest-risk things in this project are (1) does RailML actually carry the data you need, and (2) does the controller pattern hold up when real conflicts arise. Those get tested in milestones 1 and 4 respectively, before you've invested heavily in anything that depends on them being right.

## Milestone 0 — Repository scaffolding

**Duration:** a weekend.

Set up the Cargo workspace with all the crates from the architecture doc, even the ones that will be empty for months. Each crate has a `lib.rs` with a single placeholder type and a passing test. Set up CI (GitHub Actions, `cargo test` + `cargo clippy` + `cargo fmt --check`). Pick your geometry and units crates now (`uom` for units, `geo` for geographic primitives) and commit to them — changing later is painful.

**Exit criteria:** `cargo test` green, CI green, workspace boundaries enforced.

**What you're explicitly NOT doing yet:** any actual functionality. Resist the urge.

## Milestone 1 — Hand-written RailML loads into Rust types

**Duration:** 2–3 weeks.

Write a tiny RailML file by hand — three track segments, two signals, one platform. Build `sim-railml` to parse it into typed Rust structures. Build `sim-infra` to represent the loaded topology as a graph.

This is milestone 1 because RailML is the central bet of the whole architecture. Before you write a single line of physics or controller logic, you need to know: does RailML actually express what you need, and is the parsing pleasant or hostile? If RailML 3.x turns out to have gaps that matter to you (it does have gaps — interlocking detail is notoriously thin), you need to know now, not after building everything else.

**Exit criteria:** `cargo run --bin sim-runner -- load fixtures/tiny.railml` prints a topology summary. Round-trip tests pass (load, re-serialise, diff). One known gap in RailML coverage is documented in a `RAILML-GAPS.md` file.

**Risks:** RailML 3.x is verbose and the Rust XML/XSD tooling is mediocre. Budget extra time. Consider `quick-xml` with hand-written deserialisers over `serde-xml-rs` for performance and control.

## Milestone 2 — Run a single train through scripted timing

**Duration:** 2 weeks.

Add `sim-engine` (event queue, time loop), `sim-events` (the Event enum), and `sim-train` (with `DynamicsMode::Scripted` only — no physics yet). Hand-write a timetable in RailML for one train across your three-segment toy network. Run the simulation. Emit an event log.

This is the first end-to-end run. It's also the first test of the event-queue design. Scripted mode first is deliberate — it lets you exercise the engine without simultaneously debugging physics integration.

**Exit criteria:** One train runs from A to B following scripted timings. Event log written to a Parquet file with columns `(sim_time, event_type, train_id, location)`. A simple Python notebook reads the log and plots a time-distance diagram. The plot looks plausible.

**What you've learned by now:** whether your event taxonomy is right. Expect to refactor the `Event` enum at least once.

## Milestone 3 — Davis-equation physics

**Duration:** 2–3 weeks.

Build `sim-physics`. Implement Davis resistance, tractive-effort curves, and a numerical integrator (RK4 is overkill for this; symplectic Euler is fine). Add `DynamicsMode::Physical`. Validate against published acceleration data for one real UK class — Class 387 is a good choice because its specs are well-documented and EMU dynamics are simpler than locomotive-hauled.

Run the same toy network from Milestone 2 with the train in `Physical` mode. The journey time should fall within a few percent of a hand-calculated reference run.

**Exit criteria:** A test that asserts the simulated 0–100mph time for a Class 387 matches published figures within 5%. Both `Physical` and `Scripted` modes work and can be mixed in a single run.

**Risks:** Tractive-effort data for UK stock is scattered across operator manuals, RSSB documents, and enthusiast sites. Budget time for data archaeology. Keep the TE curve as a piecewise-linear lookup table — analytic curves don't fit real stock.

## Milestone 4 — Signals, interlocking, and a trivial controller

**Duration:** 3–4 weeks. **This is the hardest milestone.**

Add `sim-control` with a minimal `Controller` trait. Implement `TimetableController` — it does exactly one thing: when a train approaches a signal, set the route ahead if the booked path is clear. Add the `Interlocking` type that validates route requests against current point and signal state. Build a two-track toy network with a junction so route conflicts can actually occur.

Run two trains. Make them conflict. The controller should serialise them through the junction. The event log should show one train holding while the other passes.

This is where the controller-train decoupling pattern gets its first real test. If the abstraction is wrong, you will discover it here, and the cost of refactoring is much lower than discovering it three milestones later. Pay close attention to whether train, controller, and interlocking remain genuinely independent or whether you find yourself reaching across boundaries.

**Exit criteria:** Two trains, one junction, conflict resolved correctly. The controller cannot directly read or write train state — only emit `Action`s that go through the interlocking. Unit tests for the controller use synthetic events; unit tests for the train use synthetic signal aspects.

**What you've learned by now:** whether the controller pattern is right. If you find yourself wanting to give the controller direct access to trains, stop and rethink — the pattern is fighting you for a reason.

## Milestone 5 — Real network slice from real data

**Duration:** 3–4 weeks.

Now the Python side comes in. Build the CIF → RailML converter for one route — pick something self-contained like the Bedford–Brighton Thameslink corridor or, if you want a real test, the Castlefield Corridor through Manchester (Piccadilly–Oxford Road–Deansgate). Convert one weekday's worth of timetable. Manually verify a sample of services against Real Time Trains.

Run the simulation. Compare arrival times against the booked timetable. They should match exactly when there are no perturbations.

This is where everything you've built so far gets stress-tested against real data volume and messiness. Expect to find bugs in `sim-railml` (real RailML files exercise corners your hand-written ones didn't), bugs in your physics calibration (real timings reveal where TE curves are wrong), and bugs in the controller (real junctions have route options you didn't model).

**Exit criteria:** A realistic timetable for one route runs to completion. Simulated arrival times within 30 seconds of booked times at every stop, with no perturbations active.

**Risks:** This is the most likely milestone to spawn a hidden milestone 5b. Real data always reveals architectural gaps. Plan for it.

## Milestone 6 — Perturbations and delay propagation

**Duration:** 2–3 weeks.

Build `sim-perturb`. Implement the core perturbation types: `DwellExtension`, `DepartureDelay`, `SignalFailure`, `SpeedRestriction`. Build a scenario-runner that injects a perturbation and runs N seeded replicates.

This is the milestone where the simulator starts being *useful* — until now it just reproduces the timetable. Now you can study delay propagation. Inject a 5-minute dwell extension at a key station and watch the knock-on effects ripple through the network. Compare simulated delay propagation against TRUST data for known disruptions.

**Exit criteria:** A scenario configuration file specifies one or more perturbations. The runner executes 50 seeded replicates and outputs a distribution of delays at every measurement point. Plots clearly show delay propagation along the route.

**This is the point where the project is genuinely valuable.** Everything beyond here is widening, not enabling.

## Milestone 7 — Connections, coupling, and reactive control

**Duration:** 3–4 weeks.

Add connection handling: train A waits for train B, train coupling and dividing. Implement `HoldTrain` actions in the controller. Add a second controller implementation — a `ReactiveController` that can deviate from the booked timetable in response to disruption (cancel a service, skip a stop, re-platform).

Now you can model the *real* operational decisions that affect performance. The reactive controller is where research questions get interesting: how does a given policy compare against the timetable controller under disruption? Does aggressive cancellation reduce total delay or just shift it?

**Exit criteria:** Connection scenarios run correctly (train A held until train B arrives). Coupling/dividing modelled. At least one reactive control policy implemented and compared against the timetable controller across a scenario set.

## Milestone 8 — Passengers (the moonshot)

**Duration:** open-ended.

Build `sim-passenger`. Passengers are agents with itineraries. They consume `TrainEvent`s from the event log as passive subscribers. They accumulate delay along their journeys. Now your KPIs can be passenger-weighted: "passenger-minutes of delay" rather than "train-minutes of delay".

This is a big jump. The minimal viable version uses synthetic passenger flows (rough origin-destination matrices from NRT or LENNON data). A richer version models passenger choice — if their booked train is cancelled, do they take the next service or a different route?

**Exit criteria:** Some version of "passenger-weighted delay" computed across scenarios. You decide where to stop.

## Cross-cutting concerns

A few things you'll need throughout, not as separate milestones but as ongoing work:

**Determinism testing.** From Milestone 2 onward, have a test that runs the same scenario twice and asserts identical event logs. Add it the day you start the event queue; debugging non-determinism six months in is awful.

**Visualisation.** A time-distance diagram (also called a Marey chart or train graph) is the single most useful diagnostic tool for a rail simulator. Build a basic Python plotter at Milestone 2 and improve it over time. A geographic map view is nice but lower priority — train graphs reveal more about simulation correctness.

**Test fixtures.** Every milestone produces toy RailML files and expected outputs. Commit them. Future you, debugging a regression, will be grateful.

**Documentation.** Update `ARCHITECTURE.md` after every milestone — note what changed, what gaps you found in RailML, what assumptions turned out wrong. This is your design diary; do not skip it.

## How long will this take?

Calendar time depends entirely on how much focused time you have. As a rough guide for someone working on this part-time (evenings and weekends), Milestone 5 — where it starts being useful for real questions — is probably 6–9 months in. Milestone 6 — where it's genuinely valuable for research — is 9–12 months. Milestone 8 is "year two" territory.

If that sounds slow, it's because rail simulation is genuinely hard and the surface area is enormous. The architecture is designed so you get something running early and improve it incrementally; the alternative is spending two years on infrastructure with nothing to show.

## What if you only have time for the first three milestones?

You will still have learned something valuable. You'll know whether RailML is the right input format, whether the event-queue design holds up, and whether your physics is calibrated correctly. Those are the three biggest technical risks. If they're all green, the rest is "just" engineering — substantial engineering, but no architectural surprises.

If any of them are red, you've learned that with a few weeks of effort rather than a few years.
