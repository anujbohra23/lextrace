# LexTrace v2 product walkthrough

LexTrace v2 Engineering release status: **PASS_WITH_LIMITATIONS**.
**LEXTRACE V2 ENGINEERING: FROZEN.**

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

## Deep Research control and evidence flow

Matter Deep Research uses the following bounded design:

```text
deterministic research plan and lawyer approval
    -> LexTrace retrieval and evidence acquisition
    -> model-supported evidence analysis/findings where configured
    -> deterministic reference/provenance verification
    -> coverage reassessment and bounded stopping
```

The planner and executor are deterministic, not an autonomous LLM-planned agent
loop. Model analysis is separate from execution control; this diagram does not
imply an LLM call on every research round. The separate `/research` memo workflow
has its own structured model planning. Fixed bounds and visible retrieval steps
support reproducibility, predictable cost, auditable execution, and deterministic
control around generative analysis.

Release validation processed a real-Qwen-produced finding through the production
Deep Research graph: one query, one round, one discovery, and `LIMIT_REACHED`.
Planner/executor LLM calls were zero by design. Real local `qwen3:8b` also completed
X-Ray, Red Team, evidence-bound material monitoring, and adversarial-document
validation; the verified material alert was reviewed and applied through the UI.
These are engineering smoke checks, not evidence of retrieval quality or legal
correctness. Generated release data remains private and ignored by Git.

## Release limitations

Local inference can be slow. Research remains bounded by the small indexed corpus
and incomplete citation/treatment coverage. Qwen can misclassify document text or
misinterpret real passages; the adversarial check exposed a malicious instruction
treated as a partly supported claim. No fabricated authority reached verified
evidence in the audited runs, but valid provenance alone does not establish legal
correctness. Qwen3 8B is not a legal expert, and the system is not a comprehensive
citator. Review findings and exact evidence before relying on them.
