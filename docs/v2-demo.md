# LexTrace v2 product walkthrough

This walkthrough uses a **synthetic Matter document**. The local CourtListener
index supplies the authority evidence; its small scope can leave research gaps.
No private brief or provider output is needed to read this story.

1. Build the local index and citation graph, configure an ignored `.env`, and
   start the API and frontend as described in the [README](../README.md#run-locally).
2. Create a Matter named `Smith v. Acme` in the web workspace. Upload a short
   synthetic motion containing several separately stated propositions, a
   reporter citation present in the local index, and an intentionally overstated
   proposition. Analysis extracts claims with document source spans. Confirm
   those spans and correct any extraction errors before relying on a finding.
3. Open **Argument X-Ray** and the **Evidence Matrix**. Compare each claim with
   cited, supporting, and counter-authority passages. A partially supported
   claim remains visibly uncertain; an unresolved citation is not presented as
   verified authority. Open a claim to inspect its exact evidence and source URL.
4. For a connected authority, open **Precedent Trace** and the doctrine view.
   Inspect the citation relationship, recovered context, court hierarchy, and
   conservative treatment label. A lawyer can confirm, reject, or mark a
   treatment uncertain. A graph link alone does not prove a legal treatment.
5. Review the claim's **ResearchCoverage** and gaps. Approve a bounded **Deep
   Research** plan and inspect the resulting evidence and stop reason. Run
   **Red Team** to examine independently retrieved counter-authority; unverified
   hypotheses must remain labeled as such.
6. With monitoring configured, run a small synthetic old/new corpus fixture.
   Review any **Living Matter** impact alert and its exact passage before
   applying it to the Matter. A candidate with no verified material effect
   should not produce a substantive alert.

The walkthrough describes the intended inspection path, not a claim that every
example proposition has a known outcome in the bundled engineering corpus.
Actual Matter analysis and impact judging require a configured structured-output
provider. The walkthrough can use host-local Ollama (`qwen3:8b`) without paid
provider credits; the optional OpenAI-compatible provider requires its own
credentials and availability. Local retrieval, graph coverage, and model
judgments are limited; LexTrace does not replace legal review.
