Cheatsheet:

Reference scope:
- Use the templates and question playbooks below when structuring briefs and planning interview rounds.
- Adapt sections to the request; prefer fewer well-filled sections over many empty ones.

# Brief structure

Core sections for every brief, in order:

| Section | Contents |
|---|---|
| Title and one-liner | What is being requested, in one sentence |
| Background | Who the user is, why this is needed now, current state |
| Goals | Outcomes that define success, max 3 to 5, measurable where possible |
| Non-goals | What is explicitly out of scope, to prevent scope creep |
| Deliverables | Concrete artifacts to hand over, with formats and quantities |
| Requirements | What the work must satisfy, grouped and prioritized |
| Acceptance criteria | Checkable conditions for accepting the work |
| Constraints | Budget, deadline, technology, brand, legal limits |
| Assumptions | Decisions inferred by Clari, not confirmed by the user |
| Reference materials | Embedded user images and named examples, each with what to take from it |
| Open questions | Unresolved points, each with owner: user or contractor |

Add per request type:

| Request type | Extra sections |
|---|---|
| Software project | Users and roles, user stories, platforms and environments, integrations, data and privacy, non-functional requirements, milestones |
| Design request | Brand context, audience and tone, sizes and formats, color and typography directions, usage contexts, revision rounds |
| Freelancer task | Task breakdown, definition of done per task, handover format, communication cadence, payment milestones |
| Physical product or CAD | Function and use environment, dimensions and tolerances, materials, manufacturing method, quantity |

# Question playbooks

Ask in this priority order; skip what attachments or earlier answers already cover.

Any request:
1. What outcome should exist when this is done, and who is it for?
2. What does the recipient of the brief do: developer, designer, agency, freelancer marketplace?
3. Deadline and budget range?
4. What already exists: current site, brand assets, prior attempts, examples they like or dislike?
5. How will you judge the result is good?

Software project:
- Who are the user types and what does each need to do?
- Web, mobile, desktop? Which platforms and browsers matter?
- Must-have features for the first version versus later?
- Existing systems to integrate with: auth, payments, CRM, APIs?
- Where does data live; any privacy or compliance needs?
- Who maintains it afterwards; what stack or hosting preferences?
- Expected scale: users, traffic, content volume?

Design request:
- What is the single message the design must communicate?
- Where will it be used: print, screen, signage, packaging? Required sizes and file formats?
- Existing brand guidelines, or is this defining the brand?
- 2 to 3 references they like and why; anything to avoid?
- How many concepts and revision rounds do they expect?

Freelancer task:
- What is the smallest complete unit of work to delegate?
- What does done mean for each unit, and who reviews it?
- What access, credentials, or materials must be handed over?
- One-off or recurring; hourly or fixed price?

# Elicitation heuristics

- One topic per round: do not mix budget questions with feature questions in the same round.
- Quantify vagueness: "fast" becomes a load-time or response-time number; "modern" becomes 2 to 3 named references; "cheap" becomes a currency range.
- Offer defaults: when the user does not care, propose an industry-standard answer, record it under Assumptions, and move on.
- Closed follow-ups after open questions: start broad ("walk me through how a customer would use this"), then confirm specifics ("so checkout must support guest orders, correct?").
- Prioritize with must, should, could; the brief must make priority visible to the contractor.
- Acceptance criteria pattern: situation, action, observable result. Example: "On a phone, the gallery page loads in under 3 seconds on 4G."
- Surface hidden work: content writing, photography, translations, hosting, app store accounts, maintenance; ask who provides each.
- For images the user provides, extract: subject, style, layout, colors, and which of those they want kept versus changed.
- Stop asking when the marginal question would not change what the contractor builds; perfect is not the goal, unambiguous is.
